"""Phase 10B: Production Observability Baseline — ops endpoint and MetricsCollector tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeUsageLog,
    KnowledgeItem,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
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


def _seed_basic(db):
    """Seed minimal project, user, session, task rows."""
    user = User(email="ops@test.com", hashed_password="x", is_active=True)
    db.add(user)
    db.flush()

    project = Project(
        name="OpsTestProject",
        workspace_path="/tmp/ops_test_project",
        user_id=user.id,
    )
    db.add(project)
    db.flush()

    session = SessionModel(
        name="ops-session",
        project_id=project.id,
        status="completed",
    )
    db.add(session)
    db.flush()

    task = Task(
        title="ops-task",
        description="desc",
        project_id=project.id,
    )
    db.add(task)
    db.flush()

    return user, project, session, task


# ---------------------------------------------------------------------------
# MetricsCollector unit tests
# ---------------------------------------------------------------------------


class TestMetricsCollectorEmptyDB:
    def test_phase_latency_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.phase_latency(days=1)
        assert result["planning"]["sample_count"] == 0
        assert result["planning"]["mean_seconds"] is None
        assert result["execution"]["sample_count"] == 0
        assert result["repair"]["sample_count"] == 0

    def test_repair_stats_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.repair_stats(days=1)
        assert result["sessions_with_repair"] == 0
        assert result["repair_success_rate"] is None
        assert result["total_repair_events"] == 0

    def test_model_lane_distribution_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.model_lane_distribution(days=1)
        assert result == {
            "total_sessions": 0,
            "labels": {},
            "capability_tiers": {},
            "unknown_count": 0,
        }

    def test_retry_distribution_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.retry_distribution(days=7)
        assert result["total_executions"] == 0
        assert result["max_attempt"] == 0
        assert result["distribution"] == {}

    def test_review_policy_outcomes_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.review_policy_outcomes(days=1)
        assert result["auto_promote"] == 0
        assert result["hold_for_review"] == 0
        assert result["allow_with_warning"] == 0

    def test_operator_decisions_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.operator_decisions(days=1) == {}

    def test_rollback_count_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.rollback_count(days=1) == 0

    def test_mutation_lock_conflicts_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.mutation_lock_conflicts(days=1) == 0

    def test_qdrant_fallback_count_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.qdrant_fallback_count(days=1) == 0

    def test_openclaw_timeout_count_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.openclaw_timeout_count(days=1) == 0

    def test_failure_class_distribution_empty(self, mem_db):
        mc = MetricsCollector(mem_db)
        assert mc.failure_class_distribution() == []

    def test_storage_stats_no_projects(self, mem_db):
        mc = MetricsCollector(mem_db)
        result = mc.storage_stats([])
        assert result["total_bytes"] == 0
        assert result["per_project"] == []


class TestMetricsCollectorWithData:
    def test_phase_latency_planning_from_log_metadata(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        entry = LogEntry(
            session_id=session.id,
            level="INFO",
            message="planning done",
            log_metadata=json.dumps({"planning_duration": 42.5}),
            created_at=now,
        )
        mem_db.add(entry)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.phase_latency(days=1)
        assert result["planning"]["sample_count"] == 1
        assert result["planning"]["mean_seconds"] == 42.5

    def test_phase_latency_repair_from_log_metadata(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        entry = LogEntry(
            session_id=session.id,
            level="INFO",
            message="repair done",
            log_metadata=json.dumps(
                {
                    "retry": "repair_prompt",
                    "duration_seconds": 15.0,
                }
            ),
            created_at=now,
        )
        mem_db.add(entry)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.phase_latency(days=1)
        assert result["repair"]["sample_count"] == 1
        assert result["repair"]["mean_seconds"] == 15.0

    def test_phase_latency_execution_from_task_execution(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.DONE,
            started_at=now - timedelta(seconds=30),
            completed_at=now,
            created_at=now,
        )
        mem_db.add(execution)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.phase_latency(days=1)
        assert result["execution"]["sample_count"] == 1
        assert abs(result["execution"]["mean_seconds"] - 30.0) < 1.0

    def test_phase_latency_ignores_old_entries(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        old = datetime.now(UTC) - timedelta(days=5)

        entry = LogEntry(
            session_id=session.id,
            level="INFO",
            message="old planning",
            log_metadata=json.dumps({"planning_duration": 99.0}),
            created_at=old,
        )
        mem_db.add(entry)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.phase_latency(days=1)
        assert result["planning"]["sample_count"] == 0

    def test_retry_distribution_counts_attempts(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        # Two tasks so attempt_number uniqueness is per (session, task)
        task2 = Task(title="ops-task-2", project_id=project.id)
        mem_db.add(task2)
        mem_db.flush()

        rows = [
            (task.id, 1),
            (task2.id, 1),  # two attempt=1 across different tasks
            (task.id, 2),
            (task.id, 3),
        ]
        for task_id, attempt in rows:
            execution = TaskExecution(
                session_id=session.id,
                task_id=task_id,
                attempt_number=attempt,
                status=TaskStatus.FAILED,
                created_at=now,
            )
            mem_db.add(execution)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.retry_distribution(days=7)
        assert result["total_executions"] == 4
        assert result["distribution"]["1"] == 2
        assert result["distribution"]["2"] == 1
        assert result["distribution"]["3"] == 1
        assert result["max_attempt"] == 3

    def test_review_policy_outcomes_counts_by_outcome(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.DONE,
            created_at=now,
        )
        mem_db.add(execution)
        mem_db.flush()

        for outcome in ["auto_promote", "auto_promote", "hold_for_review"]:
            cs = TaskExecutionChangeSet(
                project_id=project.id,
                task_id=task.id,
                session_id=session.id,
                task_execution_id=execution.id,
                base_snapshot_key="base",
                review_decision={"outcome": outcome},
                created_at=now,
            )
            mem_db.add(cs)
            # Need unique task_execution_id per change set
            mem_db.flush()
            # Reset for next iteration — each change set needs different execution
            new_exec = TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=execution.attempt_number + 1,
                status=TaskStatus.DONE,
                created_at=now,
            )
            mem_db.add(new_exec)
            mem_db.flush()
            execution = new_exec

        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.review_policy_outcomes(days=1)
        assert result["auto_promote"] == 2
        assert result["hold_for_review"] == 1

    def test_operator_decisions_groups_by_disposition(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        for i, disposition in enumerate(["accepted", "accepted", "rejected"]):
            execution = TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=i + 1,
                status=TaskStatus.DONE,
                created_at=now,
            )
            mem_db.add(execution)
            mem_db.flush()

            cs = TaskExecutionChangeSet(
                project_id=project.id,
                task_id=task.id,
                session_id=session.id,
                task_execution_id=execution.id,
                base_snapshot_key="base",
                disposition=disposition,
                disposition_at=now,
                created_at=now,
            )
            mem_db.add(cs)
            mem_db.flush()

        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.operator_decisions(days=1)
        assert result["accepted"] == 2
        assert result["rejected"] == 1

    def test_rollback_count_counts_rejected_dispositions(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        for i in range(3):
            execution = TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=i + 1,
                status=TaskStatus.FAILED,
                created_at=now,
            )
            mem_db.add(execution)
            mem_db.flush()

            cs = TaskExecutionChangeSet(
                project_id=project.id,
                task_id=task.id,
                session_id=session.id,
                task_execution_id=execution.id,
                base_snapshot_key="base",
                disposition="rejected",
                disposition_at=now,
                created_at=now,
            )
            mem_db.add(cs)
            mem_db.flush()

        mem_db.commit()

        mc = MetricsCollector(mem_db)
        assert mc.rollback_count(days=1) == 3

    def test_mutation_lock_conflicts_counts_from_log_metadata(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        for _ in range(2):
            entry = LogEntry(
                session_id=session.id,
                level="ERROR",
                message="lock conflict",
                log_metadata=json.dumps({"reason": "project_mutation_lock_conflict"}),
                created_at=now,
            )
            mem_db.add(entry)

        # noise entry — should not count
        mem_db.add(
            LogEntry(
                session_id=session.id,
                level="INFO",
                message="normal",
                log_metadata=json.dumps({"reason": "planning_timeout"}),
                created_at=now,
            )
        )
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        assert mc.mutation_lock_conflicts(days=1) == 2

    def test_qdrant_fallback_count_from_knowledge_usage_logs(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        item = KnowledgeItem(
            id="abc123",
            title="guide",
            content="content",
            knowledge_type="guide",
            checksum="x" * 64,
        )
        mem_db.add(item)
        mem_db.flush()

        for _ in range(3):
            log = KnowledgeUsageLog(
                session_id=session.id,
                knowledge_item_id=item.id,
                trigger_phase="planning",
                retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
                confidence=0.5,
                rank=1,
                used_in_prompt=True,
                created_at=now,
            )
            mem_db.add(log)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        assert mc.qdrant_fallback_count(days=1) == 3

    def test_openclaw_timeout_count_from_log_metadata(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        mem_db.add(
            LogEntry(
                session_id=session.id,
                level="ERROR",
                message="timeout",
                log_metadata=json.dumps({"reason": "openclaw_timeout"}),
                created_at=now,
            )
        )
        mem_db.add(
            LogEntry(
                session_id=session.id,
                level="ERROR",
                message="no output",
                log_metadata=json.dumps({"terminal_reason": "no_output_timeout"}),
                created_at=now,
            )
        )
        mem_db.add(
            LogEntry(
                session_id=session.id,
                level="INFO",
                message="ok",
                log_metadata=json.dumps({"reason": "planning_timeout"}),
                created_at=now,
            )
        )
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        assert mc.openclaw_timeout_count(days=1) == 2

    def test_failure_class_distribution_top_reasons(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        for reason, count in [
            ("planning_timeout", 5),
            ("repair_output_contract_violation", 3),
        ]:
            for _ in range(count):
                mem_db.add(
                    LogEntry(
                        session_id=session.id,
                        level="ERROR",
                        message=reason,
                        log_metadata=json.dumps({"reason": reason}),
                        created_at=now,
                    )
                )
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.failure_class_distribution(days=30)
        assert len(result) >= 2
        reasons = {item["reason"]: item["count"] for item in result}
        assert reasons["planning_timeout"] == 5
        assert reasons["repair_output_contract_violation"] == 3
        # Top result is the most frequent
        assert result[0]["reason"] == "planning_timeout"

    def test_storage_stats_missing_workspace_path_skipped(self, mem_db):
        user = User(email="s@test.com", hashed_password="x", is_active=True)
        mem_db.add(user)
        mem_db.flush()

        project = Project(name="NoPath", workspace_path=None, user_id=user.id)
        mem_db.add(project)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.storage_stats([project])
        assert result["total_bytes"] == 0
        assert result["per_project"] == []

    def test_storage_stats_nonexistent_dir_counts_zero(self, mem_db):
        user = User(email="s2@test.com", hashed_password="x", is_active=True)
        mem_db.add(user)
        mem_db.flush()

        project = Project(
            name="NoDir",
            workspace_path="/tmp/nonexistent_ops_test_12345",
            user_id=user.id,
        )
        mem_db.add(project)
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.storage_stats([project])
        assert result["per_project"][0]["snapshot_bytes"] == 0
        assert result["per_project"][0]["archive_bytes"] == 0
        assert result["total_bytes"] == 0

    def test_repair_stats_counts_sessions_with_repair(self, mem_db):
        user, project, session, task = _seed_basic(mem_db)
        now = datetime.now(UTC)

        # Two repair events in same session
        for _ in range(2):
            mem_db.add(
                LogEntry(
                    session_id=session.id,
                    level="INFO",
                    message="repair",
                    log_metadata=json.dumps(
                        {"retry": "repair_prompt", "repair_attempts": 1}
                    ),
                    created_at=now,
                )
            )
        mem_db.commit()

        mc = MetricsCollector(mem_db)
        result = mc.repair_stats(days=1)
        assert result["sessions_with_repair"] == 1  # one session, two events
        assert result["total_repair_events"] == 2

    def test_model_lane_distribution_counts_labels_and_tiers(self, mem_db):
        _, project, _, _ = _seed_basic(mem_db)
        mem_db.add_all(
            [
                SessionModel(
                    name="hosted",
                    project_id=project.id,
                    status="completed",
                    model_lane_label="hosted_openai",
                    model_lane_metadata={"capability_tier": "hosted"},
                ),
                SessionModel(
                    name="local",
                    project_id=project.id,
                    status="completed",
                    model_lane_label="local_openclaw",
                    model_lane_metadata={"capability_tier": "local_constrained"},
                ),
                SessionModel(
                    name="legacy",
                    project_id=project.id,
                    status="completed",
                ),
            ]
        )
        mem_db.commit()

        result = MetricsCollector(mem_db).model_lane_distribution(days=1)

        assert result["total_sessions"] == 4
        assert result["labels"]["hosted_openai"] == 1
        assert result["labels"]["local_openclaw"] == 1
        assert result["labels"]["unknown"] == 2
        assert result["capability_tiers"]["hosted"] == 1
        assert result["capability_tiers"]["local_constrained"] == 1
        assert result["capability_tiers"]["unknown"] == 2
        assert result["unknown_count"] == 2


# ---------------------------------------------------------------------------
# Ops endpoint integration tests
# ---------------------------------------------------------------------------


class TestOpsHealthEndpoint:
    def test_health_returns_200(self, authenticated_client):
        resp = authenticated_client.get("/api/v1/ops/health")
        assert resp.status_code == 200

    def test_health_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/health").json()
        assert "status" in body
        assert "checked_at" in body
        assert "components" in body
        components = body["components"]
        for key in ("database", "redis", "qdrant", "celery"):
            assert key in components
            assert "status" in components[key]

    def test_health_status_is_valid(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/health").json()
        assert body["status"] in ("ok", "degraded", "unavailable")

    def test_health_component_statuses_are_valid(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/health").json()
        valid = {"ok", "degraded", "unavailable"}
        for component in body["components"].values():
            assert component["status"] in valid

    def test_health_database_is_ok_in_test(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/health").json()
        assert body["components"]["database"]["status"] == "ok"

    def test_health_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/ops/health")
        assert resp.status_code in (401, 403)


class TestOpsMetricsSummaryEndpoint:
    def test_metrics_summary_returns_200(self, authenticated_client):
        resp = authenticated_client.get("/api/v1/ops/metrics/summary")
        assert resp.status_code == 200

    def test_metrics_summary_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        assert "computed_at" in body
        assert "last_24h" in body
        assert "last_7d" in body
        for window in ("last_24h", "last_7d"):
            w = body[window]
            assert "phase_latency" in w
            assert "repair" in w
            assert "task1_product_health" in w
            assert "ordered_project_health" in w
            assert "model_lanes" in w
            assert "retry_distribution" in w
            assert "review_policy_outcomes" in w
            assert "operator_decisions" in w
            assert "rollback_count" in w
            assert "mutation_lock_conflicts" in w
            assert "qdrant_fallback_count" in w
            assert "openclaw_timeout_count" in w

    def test_metrics_summary_phase_latency_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        latency = body["last_24h"]["phase_latency"]
        for phase in ("planning", "execution", "repair"):
            assert phase in latency
            assert "mean_seconds" in latency[phase]
            assert "p95_seconds" in latency[phase]
            assert "sample_count" in latency[phase]

    def test_metrics_summary_repair_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        repair = body["last_24h"]["repair"]
        assert "sessions_with_repair" in repair
        assert "sessions_repair_succeeded" in repair
        assert "repair_success_rate" in repair
        assert "total_repair_events" in repair

    def test_metrics_summary_task1_product_health_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        task1_health = body["last_24h"]["task1_product_health"]
        assert "event_counters" in task1_health
        assert "ordered_project_first_task_success_rate" in task1_health
        assert "task1_bootstrap_contract_failure_rate" in task1_health
        assert "task1_execution_failure_rate" in task1_health
        assert "blocked_after_task1_count" in task1_health
        assert "blocked_after_task1_rate" in task1_health
        assert "clean_project_completion_rate" in task1_health

    def test_metrics_summary_ordered_project_health_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        health = body["last_24h"]["ordered_project_health"]
        assert "bootstrap_task_success_rate" in health
        assert "bootstrap_task_failure_rate" in health
        assert "ordered_project_completion_rate" in health
        assert "project_blocked_after_bootstrap" in health
        assert "blocked_after_bootstrap_rate" in health
        assert "task2_continuation_success_rate" in health
        assert "bootstrap_to_task2_continuation_latency" in health
        assert "verification_surface_mismatch_count" in health
        assert "verification_surface_mismatch_by_type" in health
        assert "repair_contract_rejection_rate" in health
        assert "project_blocked_after_task1" in health
        assert "blocked_after_task1_rate" in health

    def test_metrics_summary_model_lane_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/metrics/summary").json()
        model_lanes = body["last_24h"]["model_lanes"]
        assert "total_sessions" in model_lanes
        assert "labels" in model_lanes
        assert "capability_tiers" in model_lanes
        assert "unknown_count" in model_lanes

    def test_metrics_summary_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/ops/metrics/summary")
        assert resp.status_code in (401, 403)


class TestOpsFailureClassesEndpoint:
    def test_failure_classes_returns_200(self, authenticated_client):
        resp = authenticated_client.get("/api/v1/ops/failure-classes")
        assert resp.status_code == 200

    def test_failure_classes_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/failure-classes").json()
        assert "computed_at" in body
        assert "window_days" in body
        assert "top_failure_reasons" in body
        assert "total_classified" in body
        assert body["window_days"] == 30
        assert isinstance(body["top_failure_reasons"], list)
        assert isinstance(body["total_classified"], int)

    def test_failure_classes_each_item_has_reason_and_count(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/failure-classes").json()
        for item in body["top_failure_reasons"]:
            assert "reason" in item
            assert "count" in item
            assert isinstance(item["count"], int)

    def test_failure_classes_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/ops/failure-classes")
        assert resp.status_code in (401, 403)


class TestOpsStorageEndpoint:
    def test_storage_returns_200(self, authenticated_client):
        resp = authenticated_client.get("/api/v1/ops/storage")
        assert resp.status_code == 200

    def test_storage_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/storage").json()
        assert "computed_at" in body
        assert "total_snapshot_bytes" in body
        assert "total_archive_bytes" in body
        assert "total_bytes" in body
        assert "per_project" in body
        assert isinstance(body["per_project"], list)

    def test_storage_total_bytes_is_sum(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/storage").json()
        expected = body["total_snapshot_bytes"] + body["total_archive_bytes"]
        assert body["total_bytes"] == expected

    def test_storage_per_project_shape(self, authenticated_client):
        body = authenticated_client.get("/api/v1/ops/storage").json()
        for proj in body["per_project"]:
            assert "project_id" in proj
            assert "project_name" in proj
            assert "snapshot_bytes" in proj
            assert "archive_bytes" in proj
            assert "total_bytes" in proj
            assert proj["total_bytes"] == proj["snapshot_bytes"] + proj["archive_bytes"]

    def test_storage_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/ops/storage")
        assert resp.status_code in (401, 403)
