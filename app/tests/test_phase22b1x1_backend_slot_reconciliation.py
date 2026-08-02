"""Phase 22B-1X1: backend slot ownership, reconciliation, and capacity admission.

R5 evidence: a ``local_openclaw`` slot survived worker termination, so every
later dispatch failed slot acquisition while the provider reported healthy.
A slot is valid only while its recorded runtime owner is valid.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.agents.backend_capacity_admission import (
    CAPACITY_AMBIGUOUS,
    CAPACITY_OK,
    CAPACITY_RECONCILIATION_FAILED,
    CAPACITY_UNAVAILABLE,
    evaluate_backend_capacity,
)
from app.services.agents.backend_concurrency import (
    acquire_backend_slot,
    backend_slot_owned_by,
    build_owner_evidence,
    get_slot_owner_evidence,
    release_backend_slot,
    release_backend_slot_if_owner,
)
from app.services.agents.backend_slot_reconciliation import (
    DECISION_AMBIGUOUS_RETAINED,
    DECISION_RELEASED_STALE,
    DECISION_RETAINED,
    reconcile_backend_slots,
)
from app.services.execution.process_identity import current_process_start_identity

BACKEND = "local_openclaw"


class FakeRedis:
    """Minimal Redis stand-in supporting the exact primitives slots use."""

    def __init__(self):
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}
        self.fail_eval = False

    # --- plain commands -------------------------------------------------
    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def srem(self, key, member):
        self.sets.get(key, set()).discard(str(member))
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(str(field), None)
        return 1

    def ping(self):
        return True

    # --- scripted commands ----------------------------------------------
    def eval(self, script, numkeys, *args):
        if self.fail_eval:
            raise RuntimeError("redis eval unavailable")
        keys = [str(key) for key in args[:numkeys]]
        argv = [str(value) for value in args[numkeys:]]
        if "SISMEMBER" in script:
            return self._acquire(keys, argv)
        return self._fenced_release(keys, argv)

    def _acquire(self, keys, argv):
        slot_key, owner_key = keys
        member, max_slots, lease_seconds, evidence = (
            argv[0],
            int(argv[1]),
            int(argv[2]),
            argv[3],
        )
        members = self.sets.setdefault(slot_key, set())
        if member not in members and len(members) >= max_slots:
            return 0
        members.add(member)
        self.expirations[slot_key] = lease_seconds
        if evidence:
            self.hashes.setdefault(owner_key, {})[member] = evidence
            self.expirations[owner_key] = lease_seconds
        return 1

    def _fenced_release(self, keys, argv):
        slot_key, owner_key = keys
        member, expected = argv[0], argv[1]
        current = self.hashes.get(owner_key, {}).get(member)
        if expected == "":
            if current is not None:
                return 0
        elif current != expected:
            return 0
        self.sets.get(slot_key, set()).discard(member)
        self.hashes.get(owner_key, {}).pop(member, None)
        return 1


def _graph(db, *, execution_status=TaskStatus.RUNNING, alive=True, heartbeat_age=0):
    project = Project(name=f"p-{datetime.now(UTC).timestamp()}", description="x")
    db.add(project)
    db.commit()
    db.refresh(project)

    session = SessionModel(
        project_id=project.id, name=f"s-{project.id}", status="running"
    )
    db.add(session)
    task = Task(
        project_id=project.id, title="t", description="d", status=TaskStatus.RUNNING
    )
    db.add(task)
    db.commit()
    db.refresh(session)
    db.refresh(task)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=execution_status,
        backend_id=BACKEND,
        worker_hostname=socket.gethostname(),
        worker_pid=os_pid() if alive else 2**21,
        worker_process_start_identity=(
            current_process_start_identity() if alive else "dead-boot:1"
        ),
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=heartbeat_age),
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return session, execution


def os_pid() -> int:
    import os

    return os.getpid()


def _acquire(redis, session_id, *, execution_id=None, max_slots=1, alive_owner=True):
    evidence = build_owner_evidence(
        session_id=session_id,
        task_execution_id=execution_id,
        worker_hostname=socket.gethostname(),
        worker_pid=os_pid() if alive_owner else 2**21,
        worker_process_start_identity=(
            current_process_start_identity() if alive_owner else "dead-boot:1"
        ),
        acquired_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    return acquire_backend_slot(
        redis, BACKEND, session_id, max_slots, owner_evidence=evidence
    )


class TestSlotOwnerEvidence:
    def test_acquisition_records_owner_evidence(self):
        redis = FakeRedis()
        assert _acquire(redis, 111, execution_id=244) is True
        evidence = get_slot_owner_evidence(redis, BACKEND)
        record = json.loads(evidence["111"])
        assert record["session_id"] == 111
        assert record["task_execution_id"] == 244
        assert record["worker_pid"] == os_pid()
        assert record["owner_token"]

    def test_release_clears_member_and_evidence(self):
        redis = FakeRedis()
        _acquire(redis, 111, execution_id=244)
        assert release_backend_slot(redis, BACKEND, 111) is True
        assert backend_slot_owned_by(redis, BACKEND, 111) is False
        assert get_slot_owner_evidence(redis, BACKEND) == {}

    def test_capacity_still_bounded_by_max_slots(self):
        redis = FakeRedis()
        assert _acquire(redis, 1, max_slots=1) is True
        assert _acquire(redis, 2, max_slots=1) is False

    def test_fenced_release_refuses_when_evidence_changed(self):
        redis = FakeRedis()
        _acquire(redis, 111)
        observed = get_slot_owner_evidence(redis, BACKEND)["111"]
        # A live worker re-acquires the same member with new evidence.
        _acquire(redis, 111)
        assert release_backend_slot_if_owner(redis, BACKEND, 111, observed) is False
        assert backend_slot_owned_by(redis, BACKEND, 111) is True

    def test_fenced_release_of_legacy_member_without_evidence(self):
        redis = FakeRedis()
        redis.sets["orchestrator:backend_slots:local_openclaw"] = {"111"}
        assert release_backend_slot_if_owner(redis, BACKEND, 111, None) is True
        assert backend_slot_owned_by(redis, BACKEND, 111) is False


class TestReconciliation:
    def test_live_owner_slot_is_retained(self, db_session):
        session, execution = _graph(db_session)
        redis = FakeRedis()
        _acquire(redis, session.id, execution_id=execution.id)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 0
        assert result["decisions"][0]["decision"] == DECISION_RETAINED
        assert backend_slot_owned_by(redis, BACKEND, session.id) is True

    def test_terminal_owner_slot_is_released(self, db_session):
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 1
        assert result["decisions"][0]["decision"] == DECISION_RELEASED_STALE
        assert backend_slot_owned_by(redis, BACKEND, session.id) is False

    def test_missing_session_slot_is_released(self, db_session):
        redis = FakeRedis()
        _acquire(redis, 987654)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 1
        assert result["decisions"][0]["reason"] == "slot owner session no longer exists"

    def test_deleted_session_slot_is_released(self, db_session):
        session, _ = _graph(db_session)
        session.deleted_at = datetime.now(UTC)
        db_session.commit()
        redis = FakeRedis()
        _acquire(redis, session.id)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 1
        assert result["decisions"][0]["reason"] == "slot owner session is deleted"

    def test_dead_worker_process_identity_releases_slot(self, db_session):
        session, _ = _graph(db_session, alive=False, heartbeat_age=100000)
        redis = FakeRedis()
        _acquire(redis, session.id, alive_owner=False)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 1
        assert backend_slot_owned_by(redis, BACKEND, session.id) is False

    def test_fresh_heartbeat_without_identity_match_is_ambiguous(self, db_session):
        session, execution = _graph(db_session, alive=False, heartbeat_age=0)
        redis = FakeRedis()
        _acquire(redis, session.id, execution_id=execution.id, alive_owner=False)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["ambiguous"] == 1
        assert result["decisions"][0]["decision"] == DECISION_AMBIGUOUS_RETAINED
        assert backend_slot_owned_by(redis, BACKEND, session.id) is True

    def test_recent_acquisition_is_retained_inside_grace_window(self, db_session):
        redis = FakeRedis()
        acquire_backend_slot(
            redis,
            BACKEND,
            424242,
            1,
            owner_evidence=build_owner_evidence(session_id=424242),
        )

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 0
        assert result["decisions"][0]["decision"] == DECISION_RETAINED
        assert backend_slot_owned_by(redis, BACKEND, 424242) is True

    def test_reconciliation_is_idempotent(self, db_session):
        session, _ = _graph(db_session, execution_status=TaskStatus.FAILED)
        redis = FakeRedis()
        _acquire(redis, session.id)

        first = reconcile_backend_slots(db_session, redis, BACKEND)
        second = reconcile_backend_slots(db_session, redis, BACKEND)
        third = reconcile_backend_slots(db_session, redis, BACKEND)

        assert first["released_stale"] == 1
        assert second["released_stale"] == 0 and second["evaluated"] == 0
        assert third == {**second, "reconciled_at": third["reconciled_at"]}

    def test_concurrent_reacquisition_is_not_released(self, db_session, monkeypatch):
        """A live worker claiming the member mid-pass keeps its slot."""
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)

        original_eval = redis.eval

        def racing_eval(script, numkeys, *args):
            if "SISMEMBER" not in script:
                _acquire(redis, session.id)  # another worker re-acquires
            return original_eval(script, numkeys, *args)

        monkeypatch.setattr(redis, "eval", racing_eval)
        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["release_fenced"] == 1
        assert result["released_stale"] == 0
        assert backend_slot_owned_by(redis, BACKEND, session.id) is True

    def test_other_live_owner_is_never_released(self, db_session):
        live_session, live_execution = _graph(db_session)
        stale_session, _ = _graph(db_session, execution_status=TaskStatus.CANCELLED)
        redis = FakeRedis()
        _acquire(redis, live_session.id, execution_id=live_execution.id, max_slots=5)
        _acquire(redis, stale_session.id, max_slots=5)

        result = reconcile_backend_slots(db_session, redis, BACKEND)

        assert result["released_stale"] == 1
        assert backend_slot_owned_by(redis, BACKEND, live_session.id) is True
        assert backend_slot_owned_by(redis, BACKEND, stale_session.id) is False

    def test_dry_run_reports_without_mutating(self, db_session):
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)

        result = reconcile_backend_slots(db_session, redis, BACKEND, dry_run=True)

        assert result["released_stale"] == 1
        assert backend_slot_owned_by(redis, BACKEND, session.id) is True


class TestCapacityAdmission:
    def test_zero_available_capacity_fails(self, db_session):
        session, execution = _graph(db_session)
        redis = FakeRedis()
        _acquire(redis, session.id, execution_id=execution.id)

        capacity = evaluate_backend_capacity(db_session, redis, BACKEND, 1)

        assert capacity["capacity_available"] is False
        assert capacity["status_code"] == CAPACITY_UNAVAILABLE
        assert capacity["active_valid_count"] == 1
        assert capacity["available_count"] == 0

    def test_stale_lease_is_reconciled_then_admission_passes(self, db_session):
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)

        capacity = evaluate_backend_capacity(db_session, redis, BACKEND, 1)

        assert capacity["capacity_available"] is True
        assert capacity["status_code"] == CAPACITY_OK
        assert capacity["stale_reconciled_count"] == 1
        assert capacity["available_count"] == 1

    def test_ambiguous_ownership_fails_closed(self, db_session):
        session, execution = _graph(db_session, alive=False, heartbeat_age=0)
        redis = FakeRedis()
        _acquire(redis, session.id, execution_id=execution.id, alive_owner=False)

        capacity = evaluate_backend_capacity(db_session, redis, BACKEND, 4)

        assert capacity["capacity_available"] is False
        assert capacity["status_code"] == CAPACITY_AMBIGUOUS
        assert capacity["ambiguous_count"] == 1

    def test_normal_available_capacity_passes(self, db_session):
        redis = FakeRedis()
        capacity = evaluate_backend_capacity(db_session, redis, BACKEND, 1)

        assert capacity["capacity_available"] is True
        assert capacity["available_count"] == 1
        assert capacity["active_valid_count"] == 0

    def test_reconciliation_failure_fails_closed(self, db_session):
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)
        redis.fail_eval = True

        capacity = evaluate_backend_capacity(db_session, redis, BACKEND, 1)

        assert capacity["capacity_available"] is False
        assert capacity["status_code"] == CAPACITY_RECONCILIATION_FAILED

    def test_ungoverned_backend_reports_no_slot_ceiling(self, db_session):
        redis = FakeRedis()
        capacity = evaluate_backend_capacity(db_session, redis, "claude_code", None)

        assert capacity["capacity_available"] is True
        assert capacity["available_count"] is None


class TestReconciliationBoundaries:
    def test_worker_boot_reclaims_slots_of_the_previous_worker(
        self, db_session, monkeypatch
    ):
        from app.tasks import worker as worker_module

        session, _ = _graph(db_session, alive=False, heartbeat_age=100000)
        redis = FakeRedis()
        _acquire(redis, session.id, alive_owner=False)
        monkeypatch.setattr(
            "app.services.agents.backend_concurrency.make_redis_client", lambda: redis
        )

        result = worker_module.reconcile_backend_slots_on_boot(db_session)

        assert result is not None
        assert result["released_stale"] == 1
        assert backend_slot_owned_by(redis, BACKEND, session.id) is False

    def test_worker_boot_reconciliation_never_raises_on_redis_failure(
        self, db_session, monkeypatch
    ):
        from app.tasks import worker as worker_module

        def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(
            "app.services.agents.backend_concurrency.make_redis_client", _boom
        )
        assert worker_module.reconcile_backend_slots_on_boot(db_session) is None

    def test_maintenance_sweep_reports_reconciliation(self, db_session, monkeypatch):
        from app.tasks.maintenance import _reconcile_backend_slots_for_sweep

        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)
        monkeypatch.setattr(
            "app.services.agents.backend_concurrency.make_redis_client", lambda: redis
        )

        result = _reconcile_backend_slots_for_sweep(db_session)

        assert result["status"] == "completed"
        assert result["released_stale"] == 1

    def test_capacity_endpoint_reports_execution_role_capacity(
        self, authenticated_client, db_session, monkeypatch
    ):
        session, execution = _graph(db_session)
        redis = FakeRedis()
        _acquire(redis, session.id, execution_id=execution.id)
        monkeypatch.setattr(
            "app.services.agents.backend_concurrency.make_redis_client", lambda: redis
        )

        response = authenticated_client.get("/api/v1/ops/backends/capacity")

        assert response.status_code == 200
        payload = response.json()
        assert payload["redis_available"] is True
        assert "execution" in payload["roles"]
        execution_role = payload["roles"]["execution"]
        assert "max_slots" in execution_role
        assert "available_count" in execution_role
        assert "stale_reconciled_count" in execution_role

    def test_reconcile_endpoint_is_an_explicit_operator_action(
        self, authenticated_client, db_session, monkeypatch
    ):
        session, _ = _graph(db_session, execution_status=TaskStatus.DONE)
        redis = FakeRedis()
        _acquire(redis, session.id)
        monkeypatch.setattr(
            "app.services.agents.backend_concurrency.make_redis_client", lambda: redis
        )

        dry = authenticated_client.post(
            "/api/v1/ops/backends/slots/reconcile?dry_run=true"
        )
        assert dry.status_code == 200
        assert backend_slot_owned_by(redis, BACKEND, session.id) is True

        applied = authenticated_client.post("/api/v1/ops/backends/slots/reconcile")
        assert applied.status_code == 200
        assert applied.json()["released_stale"] == 1
        assert backend_slot_owned_by(redis, BACKEND, session.id) is False
