"""Focused Phase 31G-1 ownership and maintenance observability contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine, inspect, text

from app.celery_app import celery_app
from app.db_migrations import _migration_052_orphan_ownership_observability
from app.models import LogEntry, TaskStatus
from app.services.observability.maintenance_observability import (
    MAINTENANCE_COMPLETED,
    MAINTENANCE_DISPATCHED,
    MAINTENANCE_FAILED,
    MAINTENANCE_RECEIVED,
    MAINTENANCE_STARTED,
    build_sweep_result_counts,
    maintenance_health,
    record_maintenance_event,
)
from app.services.session.orphan_ownership import (
    ProcessObservation,
    evaluate_execution_ownership,
)
from app.tasks import maintenance


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)


def _execution(**overrides):
    values = {
        "id": 41,
        "session_id": 7,
        "task_id": 9,
        "status": TaskStatus.RUNNING,
        "worker_hostname": "worker-a",
        "worker_pid": 4242,
        "worker_process_start_identity": "boot-a:100",
        "heartbeat_at": NOW - timedelta(seconds=5),
        "started_at": NOW - timedelta(minutes=5),
        "completed_at": None,
        "runtime_lease_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _lease(**overrides):
    values = {
        "id": 501,
        "execution_task_id": 91,
        "execution_task_attempt_id": 901,
        "worker_id": "celery@worker-a",
        "worker_instance_id": "worker-a-instance",
        "worker_hostname": "worker-a",
        "worker_pid": 4242,
        "worker_process_start_identity": "boot-a:100",
        "lease_status": "active",
        "lease_expires_at": NOW + timedelta(seconds=20),
        "last_heartbeat_at": NOW - timedelta(seconds=5),
        "runtime_started_at": NOW - timedelta(minutes=5),
        "closed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _probe(monkeypatch, *, exists=True, start_identity="boot-a:100", state="observed"):
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda hostname, pid: ProcessObservation(
            state=state,
            exists=exists,
            process_start_identity=start_identity,
        ),
    )


def test_live_canonical_owner_requires_lease_heartbeat_and_identity(monkeypatch):
    _probe(monkeypatch)

    result = evaluate_execution_ownership(
        _execution(),
        runtime_lease=_lease(),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.ownership_classification == "LIVE_OWNER_CONFIRMED"
    assert result.recovery_eligible is False
    assert {"LEASE_ACTIVE", "HEARTBEAT_FRESH", "PROCESS_IDENTITY_MATCH"} <= set(
        result.signals
    )


def test_live_legacy_pid_with_matching_process_identity_is_not_recovered(monkeypatch):
    _probe(monkeypatch)

    result = evaluate_execution_ownership(
        _execution(),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.ownership_classification == "LIVE_OWNER_CONFIRMED"
    assert result.recovery_eligible is False


def test_pid_reuse_is_explicit_and_only_stale_records_can_recover(monkeypatch):
    _probe(monkeypatch, start_identity="boot-a:999")

    result = evaluate_execution_ownership(
        _execution(heartbeat_at=NOW - timedelta(minutes=5)),
        runtime_lease=_lease(
            lease_expires_at=NOW - timedelta(seconds=1),
            last_heartbeat_at=NOW - timedelta(minutes=5),
        ),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert "PROCESS_IDENTITY_MISMATCH" in result.signals
    assert result.recovery_eligible is True
    assert result.ownership_classification == "RECOVERY_ELIGIBLE"


def test_missing_process_requires_expired_lease_and_stale_heartbeat(monkeypatch):
    _probe(monkeypatch, exists=False, start_identity=None, state="not_found")

    result = evaluate_execution_ownership(
        _execution(heartbeat_at=NOW - timedelta(minutes=5)),
        runtime_lease=_lease(
            lease_expires_at=NOW - timedelta(seconds=1),
            last_heartbeat_at=NOW - timedelta(minutes=5),
        ),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.ownership_classification == "RECOVERY_ELIGIBLE"
    assert result.recovery_eligible is True
    assert "PROCESS_NOT_FOUND" in result.signals


def test_missing_process_with_fresh_lease_is_fail_safe(monkeypatch):
    _probe(monkeypatch, exists=False, start_identity=None, state="not_found")

    result = evaluate_execution_ownership(
        _execution(heartbeat_at=NOW - timedelta(minutes=5)),
        runtime_lease=_lease(),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.recovery_eligible is False
    assert "LEASE_ACTIVE" in result.signals
    assert "PROCESS_NOT_FOUND" in result.signals


def test_unavailable_process_probe_with_fresh_heartbeat_is_ambiguous(monkeypatch):
    _probe(monkeypatch, exists=None, start_identity=None, state="unavailable")

    result = evaluate_execution_ownership(
        _execution(),
        runtime_lease=_lease(),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.ownership_classification == "AMBIGUOUS_FAIL_SAFE"
    assert result.recovery_eligible is False


def test_stale_heartbeat_does_not_override_active_lease(monkeypatch):
    _probe(monkeypatch, exists=False, start_identity=None, state="not_found")

    result = evaluate_execution_ownership(
        _execution(heartbeat_at=NOW - timedelta(minutes=5)),
        runtime_lease=_lease(last_heartbeat_at=NOW - timedelta(minutes=5)),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.recovery_eligible is False
    assert "LEASE_ACTIVE" in result.signals
    assert "HEARTBEAT_STALE" in result.signals


def test_recent_authoritative_heartbeat_blocks_expired_lease_recovery(monkeypatch):
    _probe(monkeypatch, exists=False, start_identity=None, state="not_found")

    result = evaluate_execution_ownership(
        _execution(heartbeat_at=NOW - timedelta(seconds=5)),
        runtime_lease=_lease(
            lease_expires_at=NOW - timedelta(seconds=1),
            last_heartbeat_at=NOW - timedelta(seconds=5),
        ),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.recovery_eligible is False
    assert "LEASE_EXPIRED" in result.signals
    assert "HEARTBEAT_FRESH" in result.signals


def test_incomplete_legacy_owner_with_live_pid_fails_safe(monkeypatch):
    _probe(monkeypatch, start_identity="boot-a:100")

    result = evaluate_execution_ownership(
        _execution(worker_process_start_identity=None),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert result.ownership_classification == "AMBIGUOUS_FAIL_SAFE"
    assert result.recovery_eligible is False
    assert "OWNER_IDENTITY_INCOMPLETE" in result.signals


def test_terminal_and_unsupported_execution_states_never_recover(monkeypatch):
    _probe(monkeypatch)

    terminal = evaluate_execution_ownership(
        _execution(status=TaskStatus.DONE),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )
    unsupported = evaluate_execution_ownership(
        _execution(status="corrupted"),
        now=NOW,
        stale_after_seconds=30,
        current_hostname="worker-a",
    )

    assert terminal.ownership_classification == "TERMINAL_EXECUTION"
    assert terminal.recovery_eligible is False
    assert unsupported.ownership_classification == "AMBIGUOUS_FAIL_SAFE"
    assert unsupported.recovery_eligible is False


def test_sweep_result_counts_preserve_mixed_decisions():
    counts = build_sweep_result_counts(
        [
            {
                "ownership_classification": "LIVE_OWNER_CONFIRMED",
                "recovery_eligible": False,
            },
            {
                "ownership_classification": "TERMINAL_EXECUTION",
                "recovery_eligible": False,
            },
            {
                "ownership_classification": "AMBIGUOUS_FAIL_SAFE",
                "recovery_eligible": False,
            },
            {
                "ownership_classification": "RECOVERY_ELIGIBLE",
                "recovery_eligible": True,
            },
            {"ownership_classification": "LEASE_ACTIVE", "recovery_eligible": False},
        ],
        recovered_count=1,
    )

    assert counts == {
        "inspected_execution_count": 5,
        "recovery_eligible_count": 1,
        "recovered_count": 1,
        "skipped_live_count": 2,
        "skipped_terminal_count": 1,
        "ambiguous_fail_safe_count": 1,
        "error_count": 0,
    }


def test_maintenance_health_distinguishes_never_observed_from_configuration(db_session):
    health = maintenance_health(db_session, now=NOW)

    assert health["status"] == "degraded"
    assert health["freshness_state"] == "NEVER_OBSERVED"
    assert health["beat"]["configured"] is True
    assert health["last_orphan_sweep_completion"] is None


def test_maintenance_health_reports_recent_success_and_counters(db_session):
    for event_type, offset in (
        (MAINTENANCE_DISPATCHED, 20),
        (MAINTENANCE_RECEIVED, 19),
        (MAINTENANCE_STARTED, 18),
        (MAINTENANCE_COMPLETED, 17),
    ):
        record_maintenance_event(
            db_session,
            event_type=event_type,
            invocation_id="sweep-1",
            observed_at=NOW - timedelta(seconds=offset),
            counts=(
                {
                    "inspected_execution_count": 4,
                    "recovery_eligible_count": 1,
                    "recovered_count": 1,
                    "skipped_live_count": 2,
                    "skipped_terminal_count": 0,
                    "ambiguous_fail_safe_count": 1,
                    "error_count": 0,
                }
                if event_type == MAINTENANCE_COMPLETED
                else None
            ),
        )
    db_session.commit()

    health = maintenance_health(db_session, now=NOW)

    assert health["status"] == "ok"
    assert health["freshness_state"] == "HEALTHY"
    assert (
        health["last_orphan_sweep_received"]
        == (NOW - timedelta(seconds=19)).isoformat()
    )
    assert (
        health["last_orphan_sweep_completion"]
        == (NOW - timedelta(seconds=17)).isoformat()
    )
    assert health["last_sweep_result_counts"]["recovered_count"] == 1
    assert health["beat"]["liveness_state"] == "RECENT_DISPATCH"


def test_maintenance_health_reports_failed_and_incomplete_runs(db_session):
    record_maintenance_event(
        db_session,
        event_type=MAINTENANCE_FAILED,
        invocation_id="failed-1",
        observed_at=NOW - timedelta(seconds=10),
        error_category="database_unavailable",
    )
    record_maintenance_event(
        db_session,
        event_type=MAINTENANCE_RECEIVED,
        invocation_id="incomplete-1",
        observed_at=NOW - timedelta(seconds=5),
    )
    db_session.commit()

    health = maintenance_health(db_session, now=NOW)

    assert health["freshness_state"] == "FAILED"
    assert health["last_error_category"] == "database_unavailable"
    assert health["worker_received_without_completion"] is True


def test_maintenance_events_are_structured_and_durable(db_session):
    record_maintenance_event(
        db_session,
        event_type=MAINTENANCE_STARTED,
        invocation_id="structured-1",
        observed_at=NOW,
        worker_identity="celery@worker-a",
    )
    db_session.commit()

    row = db_session.query(LogEntry).order_by(LogEntry.id.desc()).first()
    assert row is not None
    assert row.message == "[MAINTENANCE_STARTED] orphan sweep"
    assert '"invocation_id": "structured-1"' in row.log_metadata
    assert '"worker_identity": "celery@worker-a"' in row.log_metadata


def test_canonical_maintenance_task_has_one_schedule_and_registration():
    entries = [
        name
        for name, entry in celery_app.conf.beat_schedule.items()
        if entry.get("task") == "app.tasks.maintenance.sweep_orphaned_running_sessions"
    ]
    assert entries == ["recover-orphaned-running-sessions"]
    celery_app.loader.import_default_modules()
    assert "app.tasks.maintenance.sweep_orphaned_running_sessions" in celery_app.tasks


def test_sweep_records_zero_orphan_completion_and_counters(monkeypatch):
    fake_db = MagicMock()
    events = []
    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)
    monkeypatch.setattr(
        maintenance,
        "record_maintenance_event",
        lambda _db, *, event_type, **kwargs: events.append((event_type, kwargs)),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.recover_stale_running_sessions",
        lambda _db, *, stale_after_seconds, decision_records: [],
    )

    result = maintenance.sweep_orphaned_running_sessions(stale_after_seconds=30)

    assert result["status"] == "completed"
    assert result["inspected_execution_count"] == 0
    assert result["recovered_count"] == 0
    assert [event[0] for event in events] == [
        MAINTENANCE_RECEIVED,
        MAINTENANCE_STARTED,
        MAINTENANCE_COMPLETED,
    ]
    assert fake_db.close.called


def test_sweep_publishes_mixed_decision_counts(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)
    monkeypatch.setattr(maintenance, "record_maintenance_event", lambda *a, **k: None)

    def _recover(_db, *, stale_after_seconds, decision_records):
        decision_records.extend(
            [
                {
                    "ownership_classification": "LIVE_OWNER_CONFIRMED",
                    "recovery_eligible": False,
                },
                {
                    "ownership_classification": "TERMINAL_EXECUTION",
                    "recovery_eligible": False,
                },
                {
                    "ownership_classification": "AMBIGUOUS_FAIL_SAFE",
                    "recovery_eligible": False,
                },
                {
                    "ownership_classification": "RECOVERY_ELIGIBLE",
                    "recovery_eligible": True,
                },
            ]
        )
        return [{"session_id": 7, "task_id": 9}]

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.recover_stale_running_sessions",
        _recover,
    )

    result = maintenance.sweep_orphaned_running_sessions(stale_after_seconds=30)

    assert result["inspected_execution_count"] == 4
    assert result["recovery_eligible_count"] == 1
    assert result["recovered_count"] == 1
    assert result["skipped_live_count"] == 1
    assert result["skipped_terminal_count"] == 1
    assert result["ambiguous_fail_safe_count"] == 1
    assert result["error_count"] == 0


def test_ownership_bridge_migration_is_additive_and_indexed(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase31g1.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE task_executions (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL
                )
                """
            )
        )

    _migration_052_orphan_ownership_observability(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("task_executions")
    }
    indexes = {
        index["name"] for index in inspect(engine).get_indexes("task_executions")
    }
    assert {"runtime_lease_id", "worker_process_start_identity"} <= columns
    assert "ix_task_executions_runtime_lease_id" in indexes
