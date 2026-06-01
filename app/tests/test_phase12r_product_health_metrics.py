"""Phase 12R: Product Health Metrics.

Deterministic fixture tests proving all ten Phase 12R metrics are correctly
computed from stable DB state.  No production behavior is changed.

Metrics covered:
- bootstrap_task_success_rate / bootstrap_task_failure_rate
- ordered_project_completion_rate
- project_blocked_after_bootstrap / blocked_after_bootstrap_rate
- task2_continuation_success_rate
- bootstrap_to_task2_continuation_latency
- verification_surface_mismatch_count / verification_surface_mismatch_by_type
- repair_contract_rejection_rate
- task1_* compatibility aliases preserved
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
    User,
)
from app.services.observability.metrics_collector import MetricsCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_project(db, name="proj"):
    user = User(email=f"{name}@test.com", hashed_password="x", is_active=True)
    db.add(user)
    db.flush()
    project = Project(
        name=name,
        workspace_path=f"/tmp/{name}",
        user_id=user.id,
    )
    db.add(project)
    db.flush()
    session = SessionModel(
        name=f"{name}-session",
        project_id=project.id,
        status="completed",
    )
    db.add(session)
    db.flush()
    return project, session


def _add_task(
    db, project_id, plan_position, status, started_at=None, completed_at=None
):
    now = datetime.now(UTC)
    task = Task(
        title=f"task-pos{plan_position}",
        description="desc",
        project_id=project_id,
        plan_position=plan_position,
        status=status,
        created_at=now,
        started_at=started_at,
        completed_at=completed_at,
    )
    db.add(task)
    db.flush()
    return task


def _add_event(db, session_id, event_type, extra=None):
    meta = {"event_type": event_type}
    if extra:
        meta.update(extra)
    entry = LogEntry(
        session_id=session_id,
        level="INFO",
        message=event_type,
        log_metadata=json.dumps(meta),
        created_at=datetime.now(UTC),
    )
    db.add(entry)
    db.flush()


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestOrderedProjectHealthEmptyDB:
    def test_returns_none_rates_when_no_bootstrap_tasks(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.ordered_project_health(days=7)

        assert result["bootstrap_task_total"] == 0
        assert result["bootstrap_task_success_rate"] is None
        assert result["bootstrap_task_failure_rate"] is None
        assert result["ordered_project_completion_rate"] is None
        assert result["blocked_after_bootstrap_rate"] is None
        assert result["task2_continuation_success_rate"] is None
        assert result["repair_contract_rejection_rate"] is None

    def test_latency_empty_when_no_data(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.ordered_project_health(days=7)
        latency = result["bootstrap_to_task2_continuation_latency"]
        assert latency["mean_seconds"] is None
        assert latency["p95_seconds"] is None
        assert latency["sample_count"] == 0

    def test_verification_mismatch_zero_when_no_events(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.ordered_project_health(days=7)
        assert result["verification_surface_mismatch_count"] == 0
        assert result["verification_surface_mismatch_by_type"] == {}

    def test_task1_aliases_present_even_when_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.ordered_project_health(days=7)
        assert "project_blocked_after_task1" in result
        assert "blocked_after_task1_rate" in result
        assert result["project_blocked_after_task1"] == 0


# ---------------------------------------------------------------------------
# bootstrap_task_success_rate / bootstrap_task_failure_rate
# ---------------------------------------------------------------------------


class TestBootstrapTaskRates:
    def test_success_rate_all_done(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["bootstrap_task_success_rate"] == 1.0
        assert result["bootstrap_task_failure_rate"] == 0.0

    def test_failure_rate_all_failed(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.FAILED)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["bootstrap_task_failure_rate"] == 1.0
        assert result["bootstrap_task_success_rate"] == 0.0

    def test_mixed_success_failure(self, mem_db):
        p1, _ = _seed_project(mem_db, "p1")
        p2, _ = _seed_project(mem_db, "p2")
        p3, _ = _seed_project(mem_db, "p3")
        _add_task(mem_db, p1.id, 1, TaskStatus.DONE)
        _add_task(mem_db, p2.id, 1, TaskStatus.DONE)
        _add_task(mem_db, p3.id, 1, TaskStatus.FAILED)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["bootstrap_task_total"] == 3
        assert result["bootstrap_task_success_rate"] == round(2 / 3, 3)
        assert result["bootstrap_task_failure_rate"] == round(1 / 3, 3)


# ---------------------------------------------------------------------------
# ordered_project_completion_rate
# ---------------------------------------------------------------------------


class TestOrderedProjectCompletionRate:
    def test_all_tasks_done_counts_as_complete(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["ordered_project_completion_rate"] == 1.0
        assert result["ordered_project_completion_count"] == 1

    def test_incomplete_task2_not_counted(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.FAILED)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["ordered_project_completion_rate"] == 0.0

    def test_partial_completion_across_projects(self, mem_db):
        p1, _ = _seed_project(mem_db, "p1")
        p2, _ = _seed_project(mem_db, "p2")
        _add_task(mem_db, p1.id, 1, TaskStatus.DONE)
        _add_task(mem_db, p1.id, 2, TaskStatus.DONE)
        _add_task(mem_db, p2.id, 1, TaskStatus.DONE)
        _add_task(mem_db, p2.id, 2, TaskStatus.FAILED)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["ordered_project_completion_rate"] == 0.5


# ---------------------------------------------------------------------------
# project_blocked_after_bootstrap / blocked_after_bootstrap_rate
# ---------------------------------------------------------------------------


class TestBlockedAfterBootstrap:
    def test_blocked_when_bootstrap_failed_and_task2_pending(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.FAILED)
        _add_task(mem_db, proj.id, 2, TaskStatus.PENDING)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["project_blocked_after_bootstrap"] == 1
        assert result["blocked_after_bootstrap_rate"] == 1.0

    def test_not_blocked_when_bootstrap_succeeded(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["project_blocked_after_bootstrap"] == 0
        assert result["blocked_after_bootstrap_rate"] == 0.0

    def test_task1_alias_equals_bootstrap_value(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.FAILED)
        _add_task(mem_db, proj.id, 2, TaskStatus.PENDING)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert (
            result["project_blocked_after_task1"]
            == result["project_blocked_after_bootstrap"]
        )
        assert (
            result["blocked_after_task1_rate"] == result["blocked_after_bootstrap_rate"]
        )


# ---------------------------------------------------------------------------
# task2_continuation_success_rate
# ---------------------------------------------------------------------------


class TestTask2ContinuationSuccessRate:
    def test_task2_done_after_bootstrap_done(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["task2_continuation_success_rate"] == 1.0
        assert result["task2_continuation_total"] == 1

    def test_task2_failed_after_bootstrap_done(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.FAILED)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["task2_continuation_success_rate"] == 0.0

    def test_no_task2_means_none_rate(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["task2_continuation_success_rate"] is None
        assert result["task2_continuation_total"] == 0

    def test_bootstrap_failed_not_counted_in_task2_total(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.FAILED)
        _add_task(mem_db, proj.id, 2, TaskStatus.PENDING)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["task2_continuation_total"] == 0
        assert result["task2_continuation_success_rate"] is None


# ---------------------------------------------------------------------------
# bootstrap_to_task2_continuation_latency
# ---------------------------------------------------------------------------


class TestBootstrapToTask2Latency:
    def test_latency_computed_from_timestamps(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        now = datetime.now(UTC)
        bt_completed = now - timedelta(seconds=120)
        t2_started = now - timedelta(seconds=60)

        _add_task(mem_db, proj.id, 1, TaskStatus.DONE, completed_at=bt_completed)
        _add_task(mem_db, proj.id, 2, TaskStatus.DONE, started_at=t2_started)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        latency = result["bootstrap_to_task2_continuation_latency"]
        assert latency["sample_count"] == 1
        assert latency["mean_seconds"] == 60.0
        assert latency["p95_seconds"] == 60.0

    def test_latency_none_when_timestamps_missing(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        _add_task(mem_db, proj.id, 2, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        latency = result["bootstrap_to_task2_continuation_latency"]
        assert latency["sample_count"] == 0
        assert latency["mean_seconds"] is None

    def test_latency_p95_with_multiple_samples(self, mem_db):
        now = datetime.now(UTC)
        latencies = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 200.0]
        for i, delta in enumerate(latencies):
            proj, _ = _seed_project(mem_db, f"p{i}")
            bt_done = now - timedelta(seconds=delta + 300)
            t2_start = bt_done + timedelta(seconds=delta)
            _add_task(mem_db, proj.id, 1, TaskStatus.DONE, completed_at=bt_done)
            _add_task(mem_db, proj.id, 2, TaskStatus.DONE, started_at=t2_start)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        latency = result["bootstrap_to_task2_continuation_latency"]
        assert latency["sample_count"] == 10
        assert latency["p95_seconds"] is not None
        assert latency["mean_seconds"] is not None


# ---------------------------------------------------------------------------
# verification_surface_mismatch_count / by_type
# ---------------------------------------------------------------------------


class TestVerificationSurfaceMismatch:
    def test_zero_when_no_mismatch_events(self, mem_db):
        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["verification_surface_mismatch_count"] == 0
        assert result["verification_surface_mismatch_by_type"] == {}

    def test_counts_mismatch_events_by_type(self, mem_db):
        _, session = _seed_project(mem_db, "p1")
        _add_event(
            mem_db,
            session.id,
            "verification_surface_mismatch",
            {"mismatch_type": "COMMAND_MISMATCH"},
        )
        _add_event(
            mem_db,
            session.id,
            "verification_surface_mismatch",
            {"mismatch_type": "COMMAND_MISMATCH"},
        )
        _add_event(
            mem_db,
            session.id,
            "verification_surface_mismatch",
            {"mismatch_type": "PYTHONPATH_MISMATCH"},
        )
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["verification_surface_mismatch_count"] == 3
        assert result["verification_surface_mismatch_by_type"]["COMMAND_MISMATCH"] == 2
        assert (
            result["verification_surface_mismatch_by_type"]["PYTHONPATH_MISMATCH"] == 1
        )

    def test_all_12m_mismatch_types_classifiable(self, mem_db):
        _, session = _seed_project(mem_db, "p1")
        mismatch_types = [
            "COMMAND_MISMATCH",
            "ENV_MISMATCH",
            "PYTHONPATH_MISMATCH",
            "SHELL_MODE_MISMATCH",
            "TIMEOUT_MISMATCH",
            "ARTIFACT_EXPECTATION_MISMATCH",
            "TERMINAL_EVENT_MISMATCH",
            "SCORER_ONLY_MISMATCH",
        ]
        for mtype in mismatch_types:
            _add_event(
                mem_db,
                session.id,
                "verification_surface_mismatch",
                {"mismatch_type": mtype},
            )
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["verification_surface_mismatch_count"] == 8
        for mtype in mismatch_types:
            assert result["verification_surface_mismatch_by_type"][mtype] == 1


# ---------------------------------------------------------------------------
# repair_contract_rejection_rate
# ---------------------------------------------------------------------------


class TestRepairContractRejectionRate:
    def test_zero_when_no_repair_events(self, mem_db):
        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["repair_contract_rejection_rate"] is None
        assert result["repair_contract_applied"] == 0
        assert result["repair_contract_rejected"] == 0

    def test_rejection_rate_from_events(self, mem_db):
        _, session = _seed_project(mem_db, "p1")
        _add_event(mem_db, session.id, "repair_applied")
        _add_event(mem_db, session.id, "repair_applied")
        _add_event(mem_db, session.id, "repair_applied")
        _add_event(mem_db, session.id, "repair_rejected")
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["repair_contract_applied"] == 3
        assert result["repair_contract_rejected"] == 1
        assert result["repair_contract_rejection_rate"] == 0.25

    def test_all_rejected_rate_is_one(self, mem_db):
        _, session = _seed_project(mem_db, "p1")
        _add_event(mem_db, session.id, "repair_rejected")
        _add_event(mem_db, session.id, "repair_rejected")
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert result["repair_contract_rejection_rate"] == 1.0


# ---------------------------------------------------------------------------
# Compatibility: task1_product_health unchanged
# ---------------------------------------------------------------------------


class TestTask1CompatibilityUnchanged:
    def test_task1_product_health_still_returns_old_keys(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.DONE)
        mem_db.commit()

        result = MetricsCollector(mem_db).task1_product_health(days=7)
        assert "blocked_after_task1_count" in result
        assert "clean_project_completion_rate" in result
        assert "task1_bootstrap_contract_failure_rate" in result
        assert "ordered_project_first_task_success_rate" in result

    def test_ordered_project_health_has_both_canonical_and_alias(self, mem_db):
        proj, _ = _seed_project(mem_db, "p1")
        _add_task(mem_db, proj.id, 1, TaskStatus.FAILED)
        _add_task(mem_db, proj.id, 2, TaskStatus.PENDING)
        mem_db.commit()

        result = MetricsCollector(mem_db).ordered_project_health(days=7)
        assert "project_blocked_after_bootstrap" in result
        assert "project_blocked_after_task1" in result
        assert (
            result["project_blocked_after_bootstrap"]
            == result["project_blocked_after_task1"]
        )
