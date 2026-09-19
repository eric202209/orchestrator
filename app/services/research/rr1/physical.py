"""Physical release gate for RR1 cross-run isolation.

Logical terminality and physical isolation are separate questions.  This
module answers only the second: *has the previous research run actually
drained from the runtime?*  It never writes, and it is never consulted to
decide whether autonomous work is logically over -- that remains the
lifecycle authority's exclusive answer (§8, §11).

Inspection failure is not release.  Every signal is tri-state, and any
unresolved signal produces :data:`PHYSICAL_STATE_UNCERTAIN`, which fails
closed: the next research run must not launch (§10).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.models import Project, Session as SessionModel, TaskExecution, TaskStatus
from app.services.orchestration.lifecycle.continuation_recovery import (
    ORCHESTRATION_TASK_NAME,
    _iter_inspected_requests,
)
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.project_mutation_lock import (
    _lock_can_be_reclaimed,
    _lock_path_for_project_root,
)

PHYSICALLY_BUSY = "PHYSICALLY_BUSY"
PHYSICALLY_RELEASED = "PHYSICALLY_RELEASED"
PHYSICAL_STATE_UNCERTAIN = "PHYSICAL_STATE_UNCERTAIN"

SIGNAL_CELERY_ACTIVE = "celery_active"
SIGNAL_CELERY_RESERVED = "celery_reserved"
SIGNAL_CELERY_SCHEDULED = "celery_scheduled"
SIGNAL_BACKEND_SLOT = "backend_capacity_slot"
SIGNAL_RUNTIME_OWNER = "runtime_owner"
SIGNAL_WORKSPACE_MUTATION_LOCK = "workspace_mutation_or_lock"
SIGNAL_CONTINUATION_DELIVERY = "pending_continuation_delivery"

PHYSICAL_SIGNALS = (
    SIGNAL_CELERY_ACTIVE,
    SIGNAL_CELERY_RESERVED,
    SIGNAL_CELERY_SCHEDULED,
    SIGNAL_BACKEND_SLOT,
    SIGNAL_RUNTIME_OWNER,
    SIGNAL_WORKSPACE_MUTATION_LOCK,
    SIGNAL_CONTINUATION_DELIVERY,
)


@dataclass(frozen=True)
class SignalProbe:
    """One tri-state physical signal.  ``present is None`` means unknown."""

    name: str
    present: bool | None
    detail: str | None = None

    @property
    def uncertain(self) -> bool:
        return self.present is None

    def as_evidence(self) -> dict[str, Any]:
        return {"name": self.name, "present": self.present, "detail": self.detail}


@dataclass(frozen=True)
class PhysicalReleaseObservation:
    """Aggregated physical ownership answer for one research run."""

    state: str
    observed_at: datetime
    probes: tuple[SignalProbe, ...] = ()

    @property
    def busy_signals(self) -> tuple[str, ...]:
        return tuple(probe.name for probe in self.probes if probe.present is True)

    @property
    def uncertain_signals(self) -> tuple[str, ...]:
        return tuple(probe.name for probe in self.probes if probe.present is None)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "observed_at": self.observed_at.isoformat(),
            "busy_signals": list(self.busy_signals),
            "uncertain_signals": list(self.uncertain_signals),
            "probes": [probe.as_evidence() for probe in self.probes],
        }


@dataclass
class RunPhysicalIdentity:
    """What "this run" means to a physical probe."""

    session_id: int
    task_id: int | None = None
    backend_ids: tuple[str, ...] = ()
    task_names: tuple[str, ...] = (ORCHESTRATION_TASK_NAME,)
    extra: dict[str, Any] = field(default_factory=dict)


def _request_matches_run(
    request: dict[str, Any], identity: RunPhysicalIdentity
) -> bool:
    """Match any orchestration delivery belonging to this run's Session.

    Deliberately broader than the BR2 continuation-identity match: a research
    run is not released while *any* orchestration delivery for its Session is
    still carried, including one for a different attempt or generation.
    """

    if str(request.get("name") or "") not in identity.task_names:
        return False
    kwargs = request.get("kwargs")
    if isinstance(kwargs, str):
        try:
            kwargs = json.loads(kwargs)
        except ValueError:
            return False
    if not isinstance(kwargs, dict):
        return False
    if kwargs.get("session_id") != identity.session_id:
        return False
    if identity.task_id is not None and kwargs.get("task_id") not in (
        None,
        identity.task_id,
    ):
        return False
    return True


def probe_celery_channels(
    identity: RunPhysicalIdentity,
    *,
    inspector: Any | None = None,
    timeout: float | None = None,
) -> tuple[SignalProbe, ...]:
    """Probe active/reserved/scheduled for this run, failing closed per channel."""

    channels = (
        ("active", SIGNAL_CELERY_ACTIVE),
        ("reserved", SIGNAL_CELERY_RESERVED),
        ("scheduled", SIGNAL_CELERY_SCHEDULED),
    )
    if inspector is None:
        try:
            from app.celery_app import celery_app

            inspector = celery_app.control.inspect(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - inspection failure is uncertainty
            return tuple(
                SignalProbe(name, None, f"inspector_unavailable:{type(exc).__name__}")
                for _, name in channels
            )

    probes: list[SignalProbe] = []
    for source, name in channels:
        probe = getattr(inspector, source, None)
        if not callable(probe):
            probes.append(SignalProbe(name, None, "channel_unavailable"))
            continue
        try:
            payload = probe()
        except Exception as exc:  # noqa: BLE001
            probes.append(SignalProbe(name, None, f"probe_error:{type(exc).__name__}"))
            continue
        if payload is None:
            # No worker replied: unknown, not empty.
            probes.append(SignalProbe(name, None, "no_worker_inspection_response"))
            continue
        matched = any(
            _request_matches_run(request, identity)
            for request in _iter_inspected_requests(payload)
        )
        probes.append(SignalProbe(name, bool(matched)))
    return tuple(probes)


def probe_backend_slot(
    identity: RunPhysicalIdentity, *, redis_client: Any | None = None
) -> SignalProbe:
    """Whether this run's Session still holds a backend capacity slot."""

    if not identity.backend_ids:
        return SignalProbe(SIGNAL_BACKEND_SLOT, False, "no_backend_declared")
    try:
        from app.services.agents.backend_concurrency import (
            backend_slot_owned_by,
            make_redis_client,
        )

        client = redis_client if redis_client is not None else make_redis_client()
        if client is None:
            return SignalProbe(SIGNAL_BACKEND_SLOT, None, "redis_unavailable")
        for backend_id in identity.backend_ids:
            if backend_slot_owned_by(client, backend_id, identity.session_id):
                return SignalProbe(SIGNAL_BACKEND_SLOT, True, backend_id)
        return SignalProbe(SIGNAL_BACKEND_SLOT, False)
    except Exception as exc:  # noqa: BLE001
        return SignalProbe(
            SIGNAL_BACKEND_SLOT, None, f"probe_error:{type(exc).__name__}"
        )


def probe_runtime_owner(db: DbSession, identity: RunPhysicalIdentity) -> SignalProbe:
    """Whether a non-terminal execution still records a live runtime owner."""

    try:
        rows = (
            db.query(
                TaskExecution.id,
                TaskExecution.status,
                TaskExecution.worker_pid,
                TaskExecution.worker_hostname,
                TaskExecution.runtime_lease_id,
            )
            .filter(TaskExecution.session_id == identity.session_id)
            .all()
        )
    except (SQLAlchemyError, AttributeError, TypeError) as exc:
        return SignalProbe(
            SIGNAL_RUNTIME_OWNER, None, f"query_error:{type(exc).__name__}"
        )
    for row in rows:
        status = getattr(row.status, "value", row.status)
        if status in {
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
        } and any((row.worker_pid, row.worker_hostname, row.runtime_lease_id)):
            return SignalProbe(
                SIGNAL_RUNTIME_OWNER, True, f"task_execution_id={row.id}"
            )
    return SignalProbe(SIGNAL_RUNTIME_OWNER, False)


def probe_workspace_mutation_or_lock(
    db: DbSession, identity: RunPhysicalIdentity
) -> SignalProbe:
    """Inspect the Product's canonical project mutation lock read-only.

    The lock path is derived from the Session's ProjectRoot, so a lock in an
    unrelated workspace is never attributed to this run.  The Product lock's
    own reclaim rule is reused: a live PID is busy, a dead/stale owner is
    released, and an unreadable or identity-mismatched lock is uncertain.
    This probe never reclaims, creates, or removes a lock.
    """

    try:
        session = (
            db.query(SessionModel)
            .filter(SessionModel.id == identity.session_id)
            .first()
        )
        if session is None:
            return SignalProbe(
                SIGNAL_WORKSPACE_MUTATION_LOCK, None, "session_not_found"
            )
        project = db.query(Project).filter(Project.id == session.project_id).first()
        if project is None:
            return SignalProbe(
                SIGNAL_WORKSPACE_MUTATION_LOCK, None, "project_not_found"
            )
        project_root = resolve_project_workspace_path(
            project.workspace_path,
            project.name,
            db=db,
        ).resolve()
        lock_path = _lock_path_for_project_root(project_root)
    except Exception as exc:  # noqa: BLE001 - measurement must fail closed
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            f"identity_inspection_error:{type(exc).__name__}",
        )

    try:
        lock_path.stat()
    except FileNotFoundError:
        return SignalProbe(SIGNAL_WORKSPACE_MUTATION_LOCK, False, "lock_absent")
    except OSError as exc:
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            f"lock_stat_error:{type(exc).__name__}",
        )

    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        metadata = None
        metadata_error = f"lock_read_error:{type(exc).__name__}"
    else:
        metadata_error = None

    if not isinstance(metadata, dict):
        # Match the Product stale-lock rule for an old unreadable lock, but
        # remain uncertain while a fresh unreadable lock could still belong
        # to a live mutation.
        try:
            stale = _lock_can_be_reclaimed(
                lock_path,
                now=time.time(),
                stale_after_seconds=6 * 60 * 60,
            )
        except OSError:
            stale = False
        if stale:
            return SignalProbe(
                SIGNAL_WORKSPACE_MUTATION_LOCK,
                False,
                "product_stale_lock_reclaimable",
            )
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            metadata_error or "lock_metadata_invalid",
        )

    expected_root = str(project_root)
    observed_root = str(metadata.get("resolved_project_root") or "").strip()
    if metadata.get("project_id") != project.id or observed_root != expected_root:
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            "lock_identity_mismatch",
        )

    try:
        stale = _lock_can_be_reclaimed(
            lock_path,
            now=time.time(),
            stale_after_seconds=6 * 60 * 60,
        )
    except OSError as exc:
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            f"stale_lock_inspection_error:{type(exc).__name__}",
        )
    if stale:
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            False,
            "product_stale_lock_reclaimable",
        )
    if "pid" not in metadata:
        return SignalProbe(
            SIGNAL_WORKSPACE_MUTATION_LOCK,
            None,
            "lock_owner_pid_missing",
        )
    return SignalProbe(
        SIGNAL_WORKSPACE_MUTATION_LOCK,
        True,
        "product_lock_owner_live_or_not_reclaimable",
    )


def probe_continuation_delivery(
    db: DbSession,
    identity: RunPhysicalIdentity,
    *,
    inspector: Any | None = None,
) -> SignalProbe:
    """Whether a durable continuation for this Session is still being carried.

    This reuses the BR2 delivery identity so the two subsystems can not
    disagree about transport.  Absence here is *not* logical terminality: a
    stranded ``retry_pending`` continuation with no visible delivery is still
    logically live, and BR2 reconciliation may restore its transport later
    (§11, §23).
    """

    from app.models import Session as SessionModel
    from app.services.orchestration.lifecycle.continuation_recovery import (
        inspect_continuation_delivery,
    )
    from app.services.orchestration.lifecycle.transitions import ContinuationIdentity

    try:
        session = (
            db.query(SessionModel)
            .filter(SessionModel.id == identity.session_id)
            .first()
        )
    except (SQLAlchemyError, AttributeError, TypeError) as exc:
        return SignalProbe(
            SIGNAL_CONTINUATION_DELIVERY, None, f"query_error:{type(exc).__name__}"
        )
    if session is None:
        return SignalProbe(SIGNAL_CONTINUATION_DELIVERY, None, "session_not_found")
    continuation_task_id = getattr(session, "continuation_task_id", None)
    continuation_kind = getattr(session, "continuation_kind", None)
    if not continuation_task_id or not continuation_kind:
        return SignalProbe(
            SIGNAL_CONTINUATION_DELIVERY, False, "no_continuation_marker"
        )

    continuation_identity = ContinuationIdentity(
        session_id=identity.session_id,
        instance_id=str(getattr(session, "instance_id", "") or ""),
        continuation_task_id=int(continuation_task_id),
        continuation_kind=str(continuation_kind),
        task_execution_id=None,
        retry_count=int(getattr(session, "continuation_retry_count", 0) or 0),
    )
    probe = inspect_continuation_delivery(continuation_identity, inspector=inspector)
    return SignalProbe(
        SIGNAL_CONTINUATION_DELIVERY,
        probe.present,
        probe.error or (",".join(probe.sources) if probe.sources else None),
    )


def classify(probes: Iterable[SignalProbe]) -> str:
    """Busy wins over uncertain; uncertain wins over released."""

    probe_list = list(probes)
    if not probe_list:
        return PHYSICAL_STATE_UNCERTAIN
    if any(probe.present is True for probe in probe_list):
        return PHYSICALLY_BUSY
    if any(probe.present is None for probe in probe_list):
        return PHYSICAL_STATE_UNCERTAIN
    return PHYSICALLY_RELEASED


def observe_physical_release(
    db: DbSession,
    identity: RunPhysicalIdentity,
    *,
    observed_at: datetime,
    inspector: Any | None = None,
    redis_client: Any | None = None,
    inspect_timeout: float | None = None,
) -> PhysicalReleaseObservation:
    """Collect every meaningful physical signal for one research run."""

    probes: list[SignalProbe] = list(
        probe_celery_channels(identity, inspector=inspector, timeout=inspect_timeout)
    )
    probes.append(probe_backend_slot(identity, redis_client=redis_client))
    probes.append(probe_runtime_owner(db, identity))
    probes.append(probe_workspace_mutation_or_lock(db, identity))
    probes.append(probe_continuation_delivery(db, identity, inspector=inspector))
    return PhysicalReleaseObservation(
        state=classify(probes),
        observed_at=observed_at,
        probes=tuple(probes),
    )
