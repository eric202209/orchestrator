"""RR1 deterministic regressions: physical release, uncertainty, isolation.

Covers RR1 sections 8 (physical release gate), 9 (stable physical drain),
10 (physical uncertainty fails closed), 11/23 (BR2 stranded-delivery
interaction), 18 (cross-run isolation) and 26 (uncertainty regression).
Every case is provider-free and uses injected inspectors, never a real broker.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.services.research.rr1.physical as rr1_physical

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import (
    derive_lifecycle_authority,
)
from app.services.orchestration.lifecycle.continuation_recovery import (
    ORCHESTRATION_TASK_NAME,
)
from app.services.research.rr1.endpoint import logical_endpoint_reached
from app.services.research.rr1.harness import (
    CENSOR_PHYSICAL_UNCERTAIN,
    CrossRunIsolationGate,
    ISOLATION_ALLOWED,
    ISOLATION_BLOCKED,
    ObservationConfig,
    RunClaim,
    RunObserver,
)
from app.services.research.rr1.manifest import DRIFT, MATCH, UNVERIFIABLE
from app.services.research.rr1.physical import (
    PHYSICAL_STATE_UNCERTAIN,
    PHYSICAL_SIGNALS,
    PHYSICALLY_BUSY,
    PHYSICALLY_RELEASED,
    RunPhysicalIdentity,
    SIGNAL_CELERY_ACTIVE,
    SIGNAL_RUNTIME_OWNER,
    SIGNAL_WORKSPACE_MUTATION_LOCK,
    SignalProbe,
    classify,
    observe_physical_release,
    probe_celery_channels,
    probe_runtime_owner,
)
from app.services.workspace.project_mutation_lock import (
    _lock_path_for_project_root,
    project_mutation_lock,
)
from app.services.research.rr1.state_machine import (
    EVIDENCE_FINALIZATION,
    PHYSICAL_RELEASE_STABILIZING,
    READY_FOR_NEXT_RUN,
    WAITING_FOR_PHYSICAL_RELEASE,
)

pytestmark = [pytest.mark.integration, pytest.mark.critical_regression]

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


class _Inspector:
    """Injected Celery inspector.  ``None`` payload means "no worker replied"."""

    def __init__(self, *, active=None, reserved=None, scheduled=None, raises=False):
        self._payloads = {
            "active": active,
            "reserved": reserved,
            "scheduled": scheduled,
        }
        self._raises = raises

    def _make(self, channel):
        def probe():
            if self._raises:
                raise ConnectionError("broker unreachable")
            return self._payloads[channel]

        return probe

    def __getattr__(self, name):
        if name in ("active", "reserved", "scheduled"):
            return self._make(name)
        raise AttributeError(name)


def _delivery(session_id: int, task_id: int | None = None) -> dict:
    return {
        "worker@host": [
            {
                "name": ORCHESTRATION_TASK_NAME,
                "kwargs": {"session_id": session_id, "task_id": task_id},
            }
        ]
    }


def _empty() -> dict:
    return {"worker@host": []}


def _seed(db, tmp_path: Path):
    workspace = tmp_path / "rr1-physical"
    workspace.mkdir(parents=True, exist_ok=True)
    project = Project(name="RR1 Physical", workspace_path=str(workspace))
    session = SessionModel(
        project=project,
        name="RR1 Physical Session",
        status="running",
        execution_mode="manual",
        is_active=True,
        instance_id="rr1-physical-generation-1",
    )
    task = Task(
        project=project,
        title="RR1 Physical Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-rr1-physical",
        workspace_status="isolated",
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session=session, task=task, attempt_number=1, status=TaskStatus.RUNNING
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    for row in (project, session, task, link, execution):
        db.refresh(row)
    return project, session, task, link, execution


def _quiesce(db, session, task, link, execution, *, status="done"):
    session.status = status
    session.continuation_task_id = None
    session.continuation_kind = None
    session.continuation_retry_count = 0
    session.continuation_retry_eta = None
    task.status = TaskStatus.DONE
    link.status = TaskStatus.DONE
    execution.status = TaskStatus.DONE
    execution.worker_pid = None
    execution.worker_hostname = None
    execution.runtime_lease_id = None
    db.commit()
    db.refresh(session)


# ---------------------------------------------------------------------------
# Section 8 / 10 -- signal classification
# ---------------------------------------------------------------------------


class TestPhysicalClassification:
    def test_frozen_physical_signal_set_has_seven_signals(self):
        assert len(PHYSICAL_SIGNALS) == 7
        assert set(PHYSICAL_SIGNALS) == {
            "celery_active",
            "celery_reserved",
            "celery_scheduled",
            "backend_capacity_slot",
            "runtime_owner",
            "workspace_mutation_or_lock",
            "pending_continuation_delivery",
        }

    def test_busy_wins_over_uncertain(self):
        assert (
            classify(
                [
                    SignalProbe("a", True),
                    SignalProbe("b", None),
                    SignalProbe("c", False),
                ]
            )
            == PHYSICALLY_BUSY
        )

    def test_uncertain_wins_over_released(self):
        assert (
            classify([SignalProbe("a", False), SignalProbe("b", None)])
            == PHYSICAL_STATE_UNCERTAIN
        )

    def test_all_absent_is_released(self):
        assert (
            classify([SignalProbe("a", False), SignalProbe("b", False)])
            == PHYSICALLY_RELEASED
        )

    def test_no_probes_is_uncertain_not_released(self):
        assert classify([]) == PHYSICAL_STATE_UNCERTAIN

    def test_matching_delivery_marks_channel_busy(self):
        probes = probe_celery_channels(
            RunPhysicalIdentity(session_id=7),
            inspector=_Inspector(
                active=_delivery(7), reserved=_empty(), scheduled=_empty()
            ),
        )
        by_name = {probe.name: probe for probe in probes}
        assert by_name[SIGNAL_CELERY_ACTIVE].present is True

    def test_no_worker_response_is_uncertain_not_empty(self):
        probes = probe_celery_channels(
            RunPhysicalIdentity(session_id=7),
            inspector=_Inspector(active=None, reserved=None, scheduled=None),
        )
        assert all(probe.present is None for probe in probes)
        assert all(probe.detail == "no_worker_inspection_response" for probe in probes)

    def test_inspection_exception_is_uncertain(self):
        probes = probe_celery_channels(
            RunPhysicalIdentity(session_id=7), inspector=_Inspector(raises=True)
        )
        assert all(probe.present is None for probe in probes)

    def test_runtime_owner_detected_from_live_execution(self, db_session, tmp_path):
        _, session, _, _, execution = _seed(db_session, tmp_path)
        execution.worker_pid = 4242
        db_session.commit()
        probe = probe_runtime_owner(
            db_session, RunPhysicalIdentity(session_id=session.id)
        )
        assert probe.name == SIGNAL_RUNTIME_OWNER
        assert probe.present is True

    def test_runtime_owner_absent_when_execution_terminal(self, db_session, tmp_path):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        probe = probe_runtime_owner(
            db_session, RunPhysicalIdentity(session_id=session.id)
        )
        assert probe.present is False

    def test_matching_live_workspace_lock_blocks_physical_release(
        self, db_session, tmp_path
    ):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        identity = RunPhysicalIdentity(session_id=session.id, task_id=task.id)
        inspector = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())

        with project_mutation_lock(
            project_id=project.id,
            project_root=Path(project.workspace_path),
            operation="rr1-test-live-lock",
            owner=f"session:{session.id}:task:{task.id}:execution:test",
            wait_timeout_seconds=0,
        ):
            observation = observe_physical_release(
                db_session,
                identity,
                observed_at=T0,
                inspector=inspector,
            )

        assert observation.state == PHYSICALLY_BUSY
        workspace_probe = next(
            probe
            for probe in observation.probes
            if probe.name == SIGNAL_WORKSPACE_MUTATION_LOCK
        )
        assert workspace_probe.present is True

    def test_no_matching_workspace_lock_is_released(self, db_session, tmp_path):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        observation = observe_physical_release(
            db_session,
            RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            observed_at=T0,
            inspector=_Inspector(
                active=_empty(), reserved=_empty(), scheduled=_empty()
            ),
        )

        assert observation.state == PHYSICALLY_RELEASED
        workspace_probe = next(
            probe
            for probe in observation.probes
            if probe.name == SIGNAL_WORKSPACE_MUTATION_LOCK
        )
        assert workspace_probe.present is False

    def test_unrelated_workspace_lock_is_not_attributed_to_run(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        unrelated_root = tmp_path / "unrelated-workspace"
        unrelated_root.mkdir()

        with project_mutation_lock(
            project_id=999,
            project_root=unrelated_root,
            operation="rr1-test-unrelated-lock",
            owner="unrelated-run",
            wait_timeout_seconds=0,
        ):
            observation = observe_physical_release(
                db_session,
                RunPhysicalIdentity(session_id=session.id, task_id=task.id),
                observed_at=T0,
                inspector=_Inspector(
                    active=_empty(), reserved=_empty(), scheduled=_empty()
                ),
            )

        assert observation.state == PHYSICALLY_RELEASED

    def test_stale_matching_workspace_lock_uses_product_reclaim_semantics(
        self, db_session, tmp_path
    ):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        project_root = Path(project.workspace_path).resolve()
        lock_path = _lock_path_for_project_root(project_root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "project_id": project.id,
                    "resolved_project_root": str(project_root),
                    "pid": 99_999_999,
                    "created_at_epoch": time.time(),
                }
            ),
            encoding="utf-8",
        )
        try:
            observation = observe_physical_release(
                db_session,
                RunPhysicalIdentity(session_id=session.id, task_id=task.id),
                observed_at=T0,
                inspector=_Inspector(
                    active=_empty(), reserved=_empty(), scheduled=_empty()
                ),
            )
        finally:
            lock_path.unlink(missing_ok=True)
            lock_path.parent.rmdir()
            lock_path.parent.parent.rmdir()

        assert observation.state == PHYSICALLY_RELEASED
        workspace_probe = next(
            probe
            for probe in observation.probes
            if probe.name == SIGNAL_WORKSPACE_MUTATION_LOCK
        )
        assert workspace_probe.present is False
        assert workspace_probe.detail == "product_stale_lock_reclaimable"

    def test_workspace_probe_is_observational(self, db_session, tmp_path):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)
        workspace = Path(project.workspace_path)
        before = {
            "session": (
                session.status,
                session.continuation_task_id,
                session.instance_id,
            ),
            "task": (task.status, task.current_step, task.error_message),
            "link": (link.status, link.started_at, link.completed_at),
            "execution": (
                execution.status,
                execution.worker_pid,
                execution.worker_hostname,
                execution.runtime_lease_id,
            ),
            "workspace_entries": sorted(
                str(path.relative_to(workspace)) for path in workspace.rglob("*")
            ),
        }

        observation = observe_physical_release(
            db_session,
            RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            observed_at=T0,
            inspector=_Inspector(
                active=_empty(), reserved=_empty(), scheduled=_empty()
            ),
        )
        db_session.refresh(session)
        db_session.refresh(task)
        db_session.refresh(link)
        db_session.refresh(execution)
        after = {
            "session": (
                session.status,
                session.continuation_task_id,
                session.instance_id,
            ),
            "task": (task.status, task.current_step, task.error_message),
            "link": (link.status, link.started_at, link.completed_at),
            "execution": (
                execution.status,
                execution.worker_pid,
                execution.worker_hostname,
                execution.runtime_lease_id,
            ),
            "workspace_entries": sorted(
                str(path.relative_to(workspace)) for path in workspace.rglob("*")
            ),
        }

        assert observation.state == PHYSICALLY_RELEASED
        assert after == before
        assert not (workspace / ".agent").exists()


# ---------------------------------------------------------------------------
# Section 9 -- stable physical drain
# ---------------------------------------------------------------------------


class TestPhysicalDrainStabilization:
    def _observer(self, session, task):
        return RunObserver(
            research_run_id="rr1-drain",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            started_at=T0,
            task_id=task.id,
            config=ObservationConfig(
                logical_stabilization_seconds=0,
                physical_stabilization_seconds=30,
                run_observation_timeout_seconds=100_000,
            ),
        )

    def test_next_run_blocked_while_delivery_present_then_drains(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        observer = self._observer(session, task)
        _quiesce(db_session, session, task, link, execution)

        # Logical endpoint accepted immediately (zero-length window).
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
            == WAITING_FOR_PHYSICAL_RELEASE
        )

        busy = _Inspector(
            active=_delivery(session.id, task.id),
            reserved=_empty(),
            scheduled=_empty(),
        )
        assert (
            observer.observe(
                db_session, session, at=T0 + timedelta(seconds=2), inspector=busy
            )
            == WAITING_FOR_PHYSICAL_RELEASE
        )
        assert observer.next_run_eligible is False

        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())
        assert (
            observer.observe(
                db_session, session, at=T0 + timedelta(seconds=3), inspector=drained
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        # Stability not yet reached.
        assert (
            observer.observe(
                db_session, session, at=T0 + timedelta(seconds=20), inspector=drained
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        assert (
            observer.observe(
                db_session, session, at=T0 + timedelta(seconds=33), inspector=drained
            )
            == EVIDENCE_FINALIZATION
        )
        assert observer.finalize(at=T0 + timedelta(seconds=34)) == READY_FOR_NEXT_RUN
        assert observer.next_run_eligible is True

    def test_reappearing_delivery_resets_physical_stabilization(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        observer = self._observer(session, task)
        _quiesce(db_session, session, task, link, execution)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=1))

        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())
        observer.observe(
            db_session, session, at=T0 + timedelta(seconds=2), inspector=drained
        )
        assert observer.machine.state == PHYSICAL_RELEASE_STABILIZING

        reappeared = _Inspector(
            active=_empty(),
            reserved=_delivery(session.id, task.id),
            scheduled=_empty(),
        )
        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=10),
                inspector=reappeared,
            )
            == WAITING_FOR_PHYSICAL_RELEASE
        )
        assert observer.physical_window.stable is False
        assert observer.physical_window.first_observed_at is None
        assert observer.next_run_eligible is False

    def test_workspace_lock_appearing_during_stabilization_resets_release(
        self, db_session, tmp_path
    ):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        observer = self._observer(session, task)
        _quiesce(db_session, session, task, link, execution)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())
        observer.observe(
            db_session, session, at=T0 + timedelta(seconds=2), inspector=drained
        )

        with project_mutation_lock(
            project_id=project.id,
            project_root=Path(project.workspace_path),
            operation="rr1-test-race-lock",
            owner=f"session:{session.id}:task:{task.id}:execution:race",
            wait_timeout_seconds=0,
        ):
            state = observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=10),
                inspector=drained,
            )
        assert state == WAITING_FOR_PHYSICAL_RELEASE
        assert observer.physical_window.stable is False
        assert observer.next_run_eligible is False

        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=11),
                inspector=drained,
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=40),
                inspector=drained,
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=41),
                inspector=drained,
            )
            == EVIDENCE_FINALIZATION
        )

    def test_workspace_release_stabilization_starts_after_lock_disappears(
        self, db_session, tmp_path
    ):
        project, session, task, link, execution = _seed(db_session, tmp_path)
        observer = self._observer(session, task)
        _quiesce(db_session, session, task, link, execution)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())

        with project_mutation_lock(
            project_id=project.id,
            project_root=Path(project.workspace_path),
            operation="rr1-test-lock-disappears",
            owner=f"session:{session.id}:task:{task.id}:execution:disappears",
            wait_timeout_seconds=0,
        ):
            assert (
                observer.observe(
                    db_session,
                    session,
                    at=T0 + timedelta(seconds=2),
                    inspector=drained,
                )
                == WAITING_FOR_PHYSICAL_RELEASE
            )

        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=20),
                inspector=drained,
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=49),
                inspector=drained,
            )
            == PHYSICAL_RELEASE_STABILIZING
        )
        assert (
            observer.observe(
                db_session,
                session,
                at=T0 + timedelta(seconds=50),
                inspector=drained,
            )
            == EVIDENCE_FINALIZATION
        )


# ---------------------------------------------------------------------------
# Section 10 / 26 -- physical uncertainty fails closed
# ---------------------------------------------------------------------------


class TestPhysicalUncertainty:
    def test_workspace_lock_inspection_failure_is_uncertain(
        self, db_session, tmp_path, monkeypatch
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)

        def fail_lock_path(_project_root):
            raise OSError("workspace lock unavailable")

        monkeypatch.setattr(rr1_physical, "_lock_path_for_project_root", fail_lock_path)
        observation = observe_physical_release(
            db_session,
            RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            observed_at=T0,
            inspector=_Inspector(
                active=_empty(), reserved=_empty(), scheduled=_empty()
            ),
        )

        assert observation.state == PHYSICAL_STATE_UNCERTAIN
        assert SIGNAL_WORKSPACE_MUTATION_LOCK in observation.uncertain_signals

    def test_logical_endpoint_stable_but_inspection_unavailable_blocks_next_run(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)

        authority = derive_lifecycle_authority(db_session, session)
        assert logical_endpoint_reached(authority) is True

        observation = observe_physical_release(
            db_session,
            RunPhysicalIdentity(session_id=session.id),
            observed_at=T0,
            inspector=_Inspector(active=None, reserved=None, scheduled=None),
        )
        assert observation.state == PHYSICAL_STATE_UNCERTAIN
        assert set(observation.uncertain_signals) >= {
            "celery_active",
            "celery_reserved",
            "celery_scheduled",
        }

        observer = RunObserver(
            research_run_id="rr1-uncertain",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id),
            started_at=T0,
            config=ObservationConfig(
                logical_stabilization_seconds=0,
                physical_stabilization_seconds=0,
                run_observation_timeout_seconds=100_000,
            ),
        )
        observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
        state = observer.observe(
            db_session,
            session,
            at=T0 + timedelta(seconds=2),
            inspector=_Inspector(active=None, reserved=None, scheduled=None),
        )
        assert state == WAITING_FOR_PHYSICAL_RELEASE
        assert observer.next_run_eligible is False

    def test_redis_slot_inspection_uncertainty_blocks_release(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution)

        class _BrokenRedis:
            def smembers(self, *_args, **_kwargs):
                raise ConnectionError("redis down")

            def hgetall(self, *_args, **_kwargs):
                raise ConnectionError("redis down")

            def sismember(self, *_args, **_kwargs):
                raise ConnectionError("redis down")

        observation = observe_physical_release(
            db_session,
            RunPhysicalIdentity(session_id=session.id, backend_ids=("backend-a",)),
            observed_at=T0,
            inspector=_Inspector(
                active=_empty(), reserved=_empty(), scheduled=_empty()
            ),
            redis_client=_BrokenRedis(),
        )
        assert observation.state == PHYSICAL_STATE_UNCERTAIN
        assert "backend_capacity_slot" in observation.uncertain_signals


# ---------------------------------------------------------------------------
# Sections 11 / 23 -- BR2 stranded-delivery interaction
# ---------------------------------------------------------------------------


class TestBr2StrandedDeliveryInteraction:
    def test_stranded_retry_pending_is_never_terminal_or_released(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        # retry_pending with a durable continuation, but no visible delivery:
        # BR2 reconciliation has not yet run.
        session.status = "retry_pending"
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        execution.status = TaskStatus.FAILED
        task.status = TaskStatus.FAILED
        link.status = TaskStatus.FAILED
        db_session.commit()
        db_session.refresh(session)

        authority = derive_lifecycle_authority(db_session, session)
        assert authority.continuation_pending is True
        assert (
            logical_endpoint_reached(authority) is False
        ), "absence of physical delivery must never make retry_pending terminal"

        observer = RunObserver(
            research_run_id="rr1-stranded",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            started_at=T0,
            task_id=task.id,
            config=ObservationConfig(
                logical_stabilization_seconds=0,
                physical_stabilization_seconds=0,
                run_observation_timeout_seconds=100_000,
            ),
        )
        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())
        state = observer.observe(
            db_session, session, at=T0 + timedelta(seconds=1), inspector=drained
        )
        assert state != READY_FOR_NEXT_RUN
        assert observer.next_run_eligible is False

    def test_harness_keeps_tracking_the_same_run_after_br2_restores_delivery(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        observer = RunObserver(
            research_run_id="rr1-br2-restore",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            started_at=T0,
            task_id=task.id,
            config=ObservationConfig(
                logical_stabilization_seconds=0,
                physical_stabilization_seconds=0,
                run_observation_timeout_seconds=100_000,
            ),
        )

        session.status = "retry_pending"
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        execution.status = TaskStatus.FAILED
        db_session.commit()
        db_session.refresh(session)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=1))

        # BR2 reconciliation restores transport; a worker claims it.
        session.status = "running"
        session.continuation_task_id = None
        session.continuation_kind = None
        session.continuation_retry_count = 0
        execution.status = TaskStatus.RUNNING
        execution.worker_pid = 9090
        db_session.commit()
        db_session.refresh(session)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=2))
        assert observer.next_run_eligible is False
        assert observer.session_id == session.id

        # The same run then completes and drains.
        _quiesce(db_session, session, task, link, execution)
        observer.observe(db_session, session, at=T0 + timedelta(seconds=3))
        drained = _Inspector(active=_empty(), reserved=_empty(), scheduled=_empty())
        observer.observe(
            db_session, session, at=T0 + timedelta(seconds=4), inspector=drained
        )
        assert observer.machine.state == EVIDENCE_FINALIZATION
        assert observer.finalize(at=T0 + timedelta(seconds=5)) == READY_FOR_NEXT_RUN
        # One observer tracked the whole run across the BR2 restoration.
        assert observer.research_run_id == "rr1-br2-restore"


# ---------------------------------------------------------------------------
# Section 18 -- cross-run isolation
# ---------------------------------------------------------------------------


class TestCrossRunIsolation:
    def _claim(self, run_id: str, **overrides) -> RunClaim:
        payload = {
            "research_run_id": run_id,
            "correlation_id": f"corr-{run_id}",
            "productroot_path": f"/roots/{run_id}",
            "workspace_path": f"/workspaces/{run_id}",
            "session_id": abs(hash(run_id)) % 100_000,
            "generation": f"gen-{run_id}",
            "backend_ids": (f"backend-{run_id}",),
        }
        payload.update(overrides)
        return RunClaim(**payload)

    def test_launch_blocked_until_previous_run_is_ready(self, db_session, tmp_path):
        _, session, task, _, _ = _seed(db_session, tmp_path)
        previous = RunObserver(
            research_run_id="run-1",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id),
            started_at=T0,
        )
        gate = CrossRunIsolationGate()
        decision = gate.evaluate(
            self._claim("run-2"), previous=previous, drift_verdict=MATCH
        )
        assert decision.decision == ISOLATION_BLOCKED
        assert any(
            reason.startswith("previous_run_not_ready") for reason in decision.reasons
        )

    def test_launch_allowed_when_previous_ready_and_no_overlap(self):
        gate = CrossRunIsolationGate()
        gate.register(self._claim("run-1"))
        decision = gate.launch(self._claim("run-2"), previous=None, drift_verdict=MATCH)
        assert decision.decision == ISOLATION_ALLOWED
        assert decision.reasons == ()

    @pytest.mark.parametrize(
        "field,label",
        [
            ("productroot_path", "productroot_overlap_with:run-1"),
            ("workspace_path", "workspace_overlap_with:run-1"),
            ("session_id", "session_overlap_with:run-1"),
            ("generation", "session_generation_overlap_with:run-1"),
            ("correlation_id", "research_correlation_identity_overlap_with:run-1"),
        ],
    )
    def test_each_resource_overlap_blocks_launch(self, field, label):
        gate = CrossRunIsolationGate()
        first = self._claim("run-1")
        gate.register(first)
        decision = gate.evaluate(
            self._claim("run-2", **{field: getattr(first, field)}),
            previous=None,
            drift_verdict=MATCH,
        )
        assert decision.decision == ISOLATION_BLOCKED
        assert label in decision.reasons

    def test_backend_runtime_ownership_overlap_blocks_launch(self):
        gate = CrossRunIsolationGate()
        gate.register(self._claim("run-1"))
        decision = gate.evaluate(
            self._claim("run-2", backend_ids=("backend-run-1",)),
            previous=None,
            drift_verdict=MATCH,
        )
        assert decision.decision == ISOLATION_BLOCKED
        assert "backend_runtime_ownership_overlap:backend-run-1" in decision.reasons

    @pytest.mark.parametrize(
        "verdict,reason",
        [
            (DRIFT, "treatment_drift"),
            (UNVERIFIABLE, "treatment_unverifiable"),
            (None, "treatment_drift_unverified"),
        ],
    )
    def test_drift_or_unverifiable_prevents_launch(self, verdict, reason):
        gate = CrossRunIsolationGate()
        decision = gate.evaluate(
            self._claim("run-2"), previous=None, drift_verdict=verdict
        )
        assert decision.decision == ISOLATION_BLOCKED
        assert reason in decision.reasons

    def test_blocked_claim_is_not_registered(self):
        gate = CrossRunIsolationGate()
        blocked = gate.launch(self._claim("run-2"), previous=None, drift_verdict=DRIFT)
        assert blocked.allowed is False
        allowed = gate.launch(self._claim("run-2"), previous=None, drift_verdict=MATCH)
        assert allowed.allowed is True, "a blocked launch must not consume the claim"
