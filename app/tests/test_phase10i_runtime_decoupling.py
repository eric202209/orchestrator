"""Phase 10I acceptance tests: runtime decoupling and lifecycle stability."""

from __future__ import annotations

import ast
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.agents.agent_runtime import BackendRole, resolve_backend_name_for_role
from app.services.agents.agent_backends import (
    get_backend_descriptor,
    list_supported_backends,
)
from app.services.agents.interfaces import RuntimeBackendResult
from app.services.agents.runtime_adapters.openclaw_adapter import (
    normalize_openclaw_execution_result,
)
from app.services.session.execution_policy import (
    classify_failure,
    resolve_ambiguous_execution,
    should_retry,
    timeout_terminal_state_blocks_late_success,
)
from app.services.agents.backend_concurrency import (
    acquire_backend_slot,
    get_concurrency_snapshot,
    release_backend_slot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_project_session_task(db_session):
    project = Project(name="TestProject10I")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id, name="TestSession10I", status="pending"
    )
    task = Task(project_id=project.id, title="TestTask10I", status=TaskStatus.PENDING)
    db_session.add_all([session, task])
    db_session.flush()
    return project, session, task


# ---------------------------------------------------------------------------
# Goal 1: Role routing
# ---------------------------------------------------------------------------


def test_backend_role_enum_values():
    assert BackendRole.PLANNING.value == "planning"
    assert BackendRole.EXECUTION.value == "execution"
    assert BackendRole.DEBUG_REPAIR.value == "debug_repair"
    assert BackendRole.REPAIR.value == "repair"


def test_resolve_backend_name_for_role_falls_back_to_agent_backend(db_session):
    """With no role-specific settings, all roles resolve to AGENT_BACKEND."""
    with patch("app.services.agents.agent_runtime.settings") as mock_settings:
        mock_settings.PLANNING_BACKEND = None
        mock_settings.EXECUTION_BACKEND = None
        mock_settings.DEBUG_REPAIR_BACKEND = None
        mock_settings.REPAIR_BACKEND = None
        mock_settings.AGENT_BACKEND = "local_openclaw"

        with patch(
            "app.services.agents.agent_runtime.get_effective_agent_backend",
            return_value="local_openclaw",
        ):
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.PLANNING)
                == "local_openclaw"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.EXECUTION)
                == "local_openclaw"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.DEBUG_REPAIR)
                == "local_openclaw"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.REPAIR)
                == "local_openclaw"
            )


def test_resolve_backend_name_for_role_uses_role_override(db_session):
    """Role-specific setting overrides AGENT_BACKEND without touching other roles."""
    with patch("app.services.agents.agent_runtime.settings") as mock_settings:
        mock_settings.PLANNING_BACKEND = "direct_ollama"
        mock_settings.EXECUTION_BACKEND = None
        mock_settings.DEBUG_REPAIR_BACKEND = None
        mock_settings.REPAIR_BACKEND = None
        mock_settings.AGENT_BACKEND = "local_openclaw"

        with patch(
            "app.services.agents.agent_runtime.get_effective_agent_backend",
            return_value="local_openclaw",
        ):
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.PLANNING)
                == "direct_ollama"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.EXECUTION)
                == "local_openclaw"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.DEBUG_REPAIR)
                == "local_openclaw"
            )
            assert (
                resolve_backend_name_for_role(db_session, BackendRole.REPAIR)
                == "local_openclaw"
            )


def test_debug_repair_backend_prefers_architecture_name_then_legacy_repair(
    db_session,
):
    """DEBUG_REPAIR_BACKEND is the architecture name; REPAIR_BACKEND remains an alias."""
    with patch("app.services.agents.agent_runtime.settings") as mock_settings:
        mock_settings.PLANNING_BACKEND = None
        mock_settings.EXECUTION_BACKEND = None
        mock_settings.DEBUG_REPAIR_BACKEND = "openai_responses_api"
        mock_settings.REPAIR_BACKEND = "direct_ollama"
        mock_settings.AGENT_BACKEND = "local_openclaw"

        assert (
            resolve_backend_name_for_role(db_session, BackendRole.DEBUG_REPAIR)
            == "openai_responses_api"
        )

        mock_settings.DEBUG_REPAIR_BACKEND = None
        assert (
            resolve_backend_name_for_role(db_session, BackendRole.DEBUG_REPAIR)
            == "direct_ollama"
        )


def test_create_agent_runtime_without_role_preserves_existing_behavior(db_session):
    """create_agent_runtime without role= keeps existing resolution path."""
    from app.services.agents.agent_runtime import create_agent_runtime

    with patch(
        "app.services.agents.agent_runtime.get_effective_agent_backend",
        return_value="local_openclaw",
    ), patch(
        "app.services.agents.agent_runtime.require_backend_descriptor"
    ) as mock_desc, patch(
        "app.services.agents.agent_runtime.get_runtime_factory"
    ) as mock_factory:
        mock_desc.return_value = MagicMock(name="local_openclaw")
        mock_runtime = MagicMock()
        mock_factory.return_value = (
            lambda db, sid, tid, use_demo_mode=None: mock_runtime
        )

        result = create_agent_runtime(db_session, session_id=1)
        assert result is mock_runtime
        mock_desc.assert_called_once_with("local_openclaw")


def test_backend_capabilities_has_max_parallel_sessions():
    descriptor = get_backend_descriptor("local_openclaw")
    assert descriptor.capabilities.max_parallel_sessions == 1


def test_non_local_backends_max_parallel_sessions_none():
    descriptor = get_backend_descriptor("direct_ollama")
    assert descriptor.capabilities.max_parallel_sessions is None


# ---------------------------------------------------------------------------
# Goal 2: Model columns
# ---------------------------------------------------------------------------


def test_task_execution_has_failure_category_column(db_session):
    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        failure_category="execution_failure",
        backend_id="local_openclaw",
    )
    db_session.add(execution)
    db_session.flush()

    retrieved = (
        db_session.query(TaskExecution).filter(TaskExecution.id == execution.id).first()
    )
    assert retrieved.failure_category == "execution_failure"
    assert retrieved.backend_id == "local_openclaw"


def test_task_execution_failure_category_nullable(db_session):
    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db_session.add(execution)
    db_session.flush()

    retrieved = (
        db_session.query(TaskExecution).filter(TaskExecution.id == execution.id).first()
    )
    assert retrieved.failure_category is None
    assert retrieved.backend_id is None


def test_session_has_escalation_backend_id_column(db_session):
    project = Project(name="EscProject10I")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id,
        name="EscSession10I",
        status="pending",
        escalation_backend_id="direct_ollama",
    )
    db_session.add(session)
    db_session.flush()

    retrieved = (
        db_session.query(SessionModel).filter(SessionModel.id == session.id).first()
    )
    assert retrieved.escalation_backend_id == "direct_ollama"


def test_session_escalation_backend_id_nullable(db_session):
    project = Project(name="EscProject10I2")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id, name="EscSession10I2", status="pending"
    )
    db_session.add(session)
    db_session.flush()

    retrieved = (
        db_session.query(SessionModel).filter(SessionModel.id == session.id).first()
    )
    assert retrieved.escalation_backend_id is None


# ---------------------------------------------------------------------------
# Goal 3: execution_policy
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    def test_capacity_keyword_maps_to_capacity_limit(self):
        assert (
            classify_failure("lock_contention", "local_openclaw", {})
            == "backend_capacity_limit"
        )

    def test_slot_keyword_maps_to_capacity_limit(self):
        assert (
            classify_failure("no_slot_available", "local_openclaw", {})
            == "backend_capacity_limit"
        )

    def test_connect_keyword_maps_to_transport_error(self):
        assert (
            classify_failure("connection_refused", "remote_openclaw_gateway", {})
            == "backend_transport_error"
        )

    def test_planning_fail_maps_to_planning_failure(self):
        assert (
            classify_failure("planning_invalid_output_error", "local_openclaw", {})
            == "planning_failure"
        )

    def test_validation_keyword_maps_to_validation_failure(self):
        assert (
            classify_failure("validator_rejected_output", "local_openclaw", {})
            == "validation_failure"
        )

    def test_governance_keyword_maps_to_governance_hold(self):
        assert (
            classify_failure("governance_hold_required", "local_openclaw", {})
            == "governance_hold"
        )

    def test_generic_failure_maps_to_execution_failure(self):
        assert (
            classify_failure("exit_code_1", "local_openclaw", {}) == "execution_failure"
        )

    def test_empty_reason_maps_to_execution_failure(self):
        assert classify_failure("", "local_openclaw", {}) == "execution_failure"

    def test_timeout_maps_to_backend_timeout(self):
        assert (
            classify_failure("Soft time limit exceeded", "local_openclaw", {})
            == "backend_timeout"
        )


class TestShouldRetry:
    def test_governance_hold_never_retries(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
        )
        db_session.add(execution)
        db_session.flush()
        assert should_retry(db_session, execution.id, "governance_hold") is False

    def test_backend_transport_error_never_retries(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
        )
        db_session.add(execution)
        db_session.flush()
        assert (
            should_retry(db_session, execution.id, "backend_transport_error") is False
        )

    def test_execution_failure_allows_retry_on_first_attempt(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
        )
        db_session.add(execution)
        db_session.flush()
        assert should_retry(db_session, execution.id, "execution_failure") is True

    def test_execution_failure_blocks_retry_after_max(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        for i in range(1, 4):
            exec_ = TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=i,
                status=TaskStatus.FAILED,
            )
            db_session.add(exec_)
        db_session.flush()
        last_exec = (
            db_session.query(TaskExecution)
            .filter(TaskExecution.session_id == session.id)
            .order_by(TaskExecution.id.desc())
            .first()
        )
        assert should_retry(db_session, last_exec.id, "execution_failure") is False


class TestResolveAmbiguousExecution:
    def test_running_execution_resolves_to_cancelled(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
        db_session.add(execution)
        db_session.flush()
        result = resolve_ambiguous_execution(db_session, execution.id, runtime=None)
        assert result == TaskStatus.CANCELLED.value

    def test_running_execution_gets_lifecycle_inconsistency_category(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
        db_session.add(execution)
        db_session.flush()
        resolve_ambiguous_execution(db_session, execution.id, runtime=None)
        assert execution.failure_category == "lifecycle_inconsistency"

    def test_non_running_execution_status_returned_unchanged(self, db_session):
        _, session, task = _make_project_session_task(db_session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.DONE,
        )
        db_session.add(execution)
        db_session.flush()
        result = resolve_ambiguous_execution(db_session, execution.id, runtime=None)
        assert result == TaskStatus.DONE.value

    def test_missing_execution_returns_failed(self, db_session):
        result = resolve_ambiguous_execution(
            db_session, task_execution_id=99999, runtime=None
        )
        assert result == TaskStatus.FAILED.value


# ---------------------------------------------------------------------------
# Goal 4: backend_concurrency
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal Redis stand-in for concurrency tests."""

    def __init__(self):
        self._sets: dict[str, set[str]] = {}
        self._expirations: dict[str, int] = {}

    def smembers(self, key: str) -> set[bytes]:
        return {v.encode() for v in self._sets.get(key, set())}

    def sadd(self, key: str, *values: str) -> int:
        self._sets.setdefault(key, set()).update(str(v) for v in values)
        return len(values)

    def srem(self, key: str, *values: str) -> int:
        s = self._sets.get(key, set())
        before = len(s)
        s.difference_update(str(v) for v in values)
        return before - len(s)

    def expire(self, key: str, ttl: int) -> None:
        self._expirations[key] = ttl

    def eval(self, _script, key_count, key, session_id, max_slots, lease_seconds):
        assert key_count == 1
        members = self._sets.setdefault(key, set())
        if str(session_id) not in members and len(members) >= int(max_slots):
            return 0
        members.add(str(session_id))
        self.expire(key, int(lease_seconds))
        return 1

    def pipeline(self):
        return _FakePipeline(self)

    def ping(self):
        return True


class _FakePipeline:
    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._commands: list = []
        self._watching = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def watch(self, key: str) -> None:
        self._watching = True

    def reset(self) -> None:
        self._commands.clear()
        self._watching = False

    def multi(self) -> None:
        self._commands = []

    def sadd(self, key: str, *values: str) -> None:
        self._commands.append(("sadd", key, values))

    def expire(self, key: str, ttl: int) -> None:
        self._commands.append(("expire", key, ttl))

    def execute(self) -> list:
        for cmd in self._commands:
            if cmd[0] == "sadd":
                self._redis.sadd(cmd[1], *cmd[2])
            elif cmd[0] == "expire":
                self._redis.expire(cmd[1], cmd[2])
        return [True] * len(self._commands)


class TestBackendConcurrency:
    def test_acquire_slot_uses_atomic_claim_and_preserves_task_length_lease(self):
        class AtomicRedis:
            def __init__(self):
                self.calls = []

            def eval(
                self, script, key_count, key, session_id, max_slots, lease_seconds
            ):
                self.calls.append(
                    (script, key_count, key, session_id, max_slots, lease_seconds)
                )
                return 1

        redis = AtomicRedis()

        assert (
            acquire_backend_slot(
                redis,
                "local_openclaw",
                session_id=7,
                max_slots=1,
                timeout_s=3600,
            )
            is True
        )
        assert redis.calls[0][1:] == (
            1,
            "orchestrator:backend_slots:local_openclaw",
            "7",
            1,
            3600,
        )

    def test_atomic_claim_returns_false_at_capacity(self):
        class FullRedis:
            def eval(self, *_args):
                return 0

        assert (
            acquire_backend_slot(
                FullRedis(), "local_openclaw", session_id=2, max_slots=1
            )
            is False
        )

    def test_acquire_slot_succeeds_when_under_max(self):
        r = FakeRedis()
        result = acquire_backend_slot(r, "local_openclaw", session_id=1, max_slots=1)
        assert result is True

    def test_acquire_slot_fails_when_at_max(self):
        r = FakeRedis()
        acquire_backend_slot(r, "local_openclaw", session_id=1, max_slots=1)
        result = acquire_backend_slot(r, "local_openclaw", session_id=2, max_slots=1)
        assert result is False

    def test_release_slot_frees_capacity(self):
        r = FakeRedis()
        acquire_backend_slot(r, "local_openclaw", session_id=1, max_slots=1)
        release_backend_slot(r, "local_openclaw", session_id=1)
        result = acquire_backend_slot(r, "local_openclaw", session_id=2, max_slots=1)
        assert result is True

    def test_snapshot_reflects_active_slots(self):
        r = FakeRedis()
        acquire_backend_slot(r, "local_openclaw", session_id=10, max_slots=2)
        acquire_backend_slot(r, "local_openclaw", session_id=20, max_slots=2)
        snap = get_concurrency_snapshot(r, "local_openclaw")
        assert snap["active_count"] == 2
        assert 10 in snap["active_session_ids"]
        assert 20 in snap["active_session_ids"]

    def test_capacity_limit_classifies_correctly(self):
        assert (
            classify_failure("backend_capacity_limit", "local_openclaw", {})
            == "backend_capacity_limit"
        )

    def test_snapshot_empty_by_default(self):
        r = FakeRedis()
        snap = get_concurrency_snapshot(r, "local_openclaw")
        assert snap["active_count"] == 0
        assert snap["active_session_ids"] == []


# ---------------------------------------------------------------------------
# Goal 5: ops endpoints
# ---------------------------------------------------------------------------


def test_ops_backends_listed_correctly(authenticated_client):
    resp = authenticated_client.get("/api/v1/ops/backends")
    assert resp.status_code == 200
    data = resp.json()
    assert "backends" in data
    names = {b["name"] for b in data["backends"]}
    assert "local_openclaw" in names
    assert "direct_ollama" in names
    local_openclaw = next(b for b in data["backends"] if b["name"] == "local_openclaw")
    assert "roles" in local_openclaw
    assert "configured_for_roles" in local_openclaw
    assert "max_parallel_sessions" in local_openclaw


def test_ops_backends_health_returns_per_backend_status(authenticated_client):
    resp = authenticated_client.get("/api/v1/ops/backends/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "backends" in data
    for entry in data["backends"]:
        assert "name" in entry
        assert "available" in entry
        assert "status" in entry


def test_ops_backends_concurrency_returns_snapshots(authenticated_client):
    resp = authenticated_client.get("/api/v1/ops/backends/concurrency")
    assert resp.status_code == 200
    data = resp.json()
    assert "backends" in data or "redis_available" in data


def test_ops_backends_concurrency_includes_capacity_and_failure_fields(
    authenticated_client, db_session, monkeypatch
):
    import app.services.agents.backend_concurrency as backend_concurrency

    r = FakeRedis()
    acquire_backend_slot(r, "local_openclaw", session_id=123, max_slots=1)
    monkeypatch.setattr(backend_concurrency, "make_redis_client", lambda: r)

    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        backend_id="local_openclaw",
        failure_category="backend_capacity_limit",
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(execution)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/ops/backends/concurrency")
    assert resp.status_code == 200
    data = resp.json()
    local_openclaw = next(
        b for b in data["backends"] if b["backend_id"] == "local_openclaw"
    )
    assert local_openclaw["max_parallel_sessions"] == 1
    assert local_openclaw["active_count"] == 1
    assert local_openclaw["capacity_available"] is False
    assert local_openclaw["last_failure_category"] == "backend_capacity_limit"


# ---------------------------------------------------------------------------
# Recovery path: resolve_ambiguous_execution wired into lifecycle service
# ---------------------------------------------------------------------------


def test_recovery_resolves_running_execution_to_lifecycle_inconsistency(db_session):
    """_stop_running_session_for_recovery sets failure_category on stale RUNNING executions."""
    from app.services.session.session_lifecycle_service import (
        _stop_running_session_for_recovery,
    )

    project = Project(name="RecoveryProject10I")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id,
        name="RecoverySession10I",
        status="running",
        is_active=True,
    )
    task = Task(
        project_id=project.id, title="RecoveryTask10I", status=TaskStatus.RUNNING
    )
    db_session.add_all([session, task])
    db_session.flush()

    from app.models import SessionTask

    stlink = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
    )
    db_session.add(stlink)
    db_session.flush()

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.flush()

    _stop_running_session_for_recovery(
        db_session,
        session=session,
        task=task,
        session_task=stlink,
        stop_reason="hard_time_limit_or_worker_killed",
        task_error_message="Stale run recovered.",
        alert_message="Stale run.",
        recovery_log_message="Recovery log.",
    )

    db_session.refresh(execution)
    assert execution.failure_category == "lifecycle_inconsistency"
    assert execution.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# update_execution_failure_metadata
# ---------------------------------------------------------------------------


def test_update_execution_failure_metadata_writes_columns(db_session):
    from app.services.session.session_execution_service import (
        update_execution_failure_metadata,
    )

    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
    )
    db_session.add(execution)
    db_session.flush()

    update_execution_failure_metadata(
        db_session,
        execution.id,
        failure_category="governance_hold",
        backend_id="local_openclaw",
    )
    db_session.flush()

    db_session.refresh(execution)
    assert execution.failure_category == "governance_hold"
    assert execution.backend_id == "local_openclaw"


def test_update_execution_failure_metadata_noop_on_missing(db_session):
    from app.services.session.session_execution_service import (
        update_execution_failure_metadata,
    )

    update_execution_failure_metadata(
        db_session, 99999, failure_category="execution_failure"
    )
    # no exception


def test_timeout_terminal_state_blocks_late_success(db_session):
    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        failure_category="backend_timeout",
        backend_id="local_openclaw",
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(execution)
    db_session.flush()

    assert timeout_terminal_state_blocks_late_success(execution) is True


def test_mark_execution_done_does_not_promote_after_backend_timeout(db_session):
    from app.models import SessionTask
    from app.services.session.session_execution_service import mark_execution_done

    _, session, task = _make_project_session_task(db_session)
    task.status = TaskStatus.FAILED
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.FAILED,
        completed_at=datetime.now(timezone.utc),
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        failure_category="backend_timeout",
        backend_id="local_openclaw",
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([link, execution])
    db_session.flush()

    mark_execution_done(
        task=task,
        session_task_link=link,
        task_execution=execution,
        completed_at=datetime.now(timezone.utc),
    )

    assert execution.status == TaskStatus.FAILED
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    assert execution.failure_category == "backend_timeout"


# ---------------------------------------------------------------------------
# Worker import sanity (regression: settings was missing from worker.py)
# ---------------------------------------------------------------------------


def test_worker_module_has_settings_and_resolve_backend():
    """worker.py must import settings and resolve_backend_name_for_role (were missing)."""
    import app.tasks.worker as worker_module
    from app.config import settings as real_settings

    assert hasattr(worker_module, "settings"), "settings not imported in worker.py"
    assert worker_module.settings is real_settings

    assert hasattr(
        worker_module, "resolve_backend_name_for_role"
    ), "resolve_backend_name_for_role not imported in worker.py"


def test_worker_uses_configured_planning_runtime_when_backend_differs():
    import app.tasks.worker as worker_module

    assert (
        worker_module.should_use_configured_planning_runtime(
            planning_backend_override=None,
            resolved_planning_backend="direct_ollama",
            resolved_execution_backend="local_openclaw",
        )
        is True
    )


def test_worker_reuses_execution_runtime_when_planning_backend_matches():
    import app.tasks.worker as worker_module

    assert (
        worker_module.should_use_configured_planning_runtime(
            planning_backend_override=None,
            resolved_planning_backend="local_openclaw",
            resolved_execution_backend="local_openclaw",
        )
        is False
    )


def test_worker_operator_planning_override_still_forces_planning_runtime():
    import app.tasks.worker as worker_module

    assert (
        worker_module.should_use_configured_planning_runtime(
            planning_backend_override="direct_ollama",
            resolved_planning_backend="local_openclaw",
            resolved_execution_backend="local_openclaw",
        )
        is True
    )


def test_backend_capacity_retry_state_marks_exhaustion():
    import app.tasks.worker as worker_module

    class Request:
        retries = worker_module.BACKEND_CAPACITY_RETRY_MAX_RETRIES

    retry_count, exhausted = worker_module.backend_capacity_retry_state(Request())

    assert retry_count == worker_module.BACKEND_CAPACITY_RETRY_MAX_RETRIES
    assert exhausted is True


def test_backend_capacity_retry_budget_is_900s():
    import app.tasks.worker as worker_module

    # Phase 19F: 60 retries x 15s countdown = 900s total patience budget
    # (raised from 300s — see BACKEND_CAPACITY_RETRY_MAX_RETRIES docstring).
    countdown_seconds = 15
    budget = worker_module.BACKEND_CAPACITY_RETRY_MAX_RETRIES * countdown_seconds
    assert worker_module.BACKEND_CAPACITY_RETRY_MAX_RETRIES == 60
    assert budget == 900


def test_prepare_backend_capacity_retry_returns_attempt_to_pending(db_session):
    import app.tasks.worker as worker_module
    from app.models import SessionTask

    _, session, task = _make_project_session_task(db_session)
    task.status = TaskStatus.RUNNING
    task.workspace_status = "not_created"
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        backend_id="local_openclaw",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([link, execution])
    db_session.flush()

    worker_module.prepare_backend_capacity_retry(
        task=task,
        session_task_link=link,
        task_execution=execution,
        backend_id="local_openclaw",
    )

    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.PENDING
    assert execution.failure_category == "backend_capacity_limit"
    assert execution.backend_id == "local_openclaw"
    assert execution.completed_at is None


def test_classify_failure_defaults_to_execution_failure_for_unknown_reason():
    """Arbitrary exception messages that match no pattern → execution_failure."""
    result = classify_failure("something went wrong", "local_openclaw", {})
    assert result == "execution_failure"


def test_acquire_backend_slot_propagates_redis_errors():
    """Redis operational errors must escape acquire_backend_slot so worker can fail open.

    Previously the function swallowed all exceptions and returned False, which made
    Redis-down indistinguishable from 'at capacity' and triggered unintended retries.
    """

    class BrokenRedis:
        def eval(self, *_args):
            raise OSError("redis down")

    with pytest.raises(OSError):
        acquire_backend_slot(BrokenRedis(), "local_openclaw", session_id=1, max_slots=1)


def test_planning_and_execution_can_resolve_independently():
    """Prove planning and execution resolve different backends via config with no code change."""
    with patch("app.services.agents.agent_runtime.settings") as mock_settings:
        mock_settings.PLANNING_BACKEND = "direct_ollama"
        mock_settings.EXECUTION_BACKEND = "openai_responses_api"
        mock_settings.REPAIR_BACKEND = None
        mock_settings.AGENT_BACKEND = "local_openclaw"

        planning_backend = resolve_backend_name_for_role(None, BackendRole.PLANNING)
        execution_backend = resolve_backend_name_for_role(None, BackendRole.EXECUTION)

    assert planning_backend == "direct_ollama"
    assert execution_backend == "openai_responses_api"
    assert planning_backend != execution_backend


# ---------------------------------------------------------------------------
# Phase 10I-post: execution backend result contract
# ---------------------------------------------------------------------------


def test_runtime_backend_result_to_dict_is_contract_shape():
    result = RuntimeBackendResult(
        backend_id="local_openclaw",
        role="execution",
        success=False,
        exit_reason="backend_at_capacity",
        output=None,
        duration_seconds=1.25,
        failure_category="backend_capacity_limit",
        terminal_reason="retry_later",
    )

    assert result.to_dict() == {
        "backend_id": "local_openclaw",
        "role": "execution",
        "success": False,
        "exit_reason": "backend_at_capacity",
        "output": None,
        "duration_seconds": 1.25,
        "failure_category": "backend_capacity_limit",
        "terminal_reason": "retry_later",
        "tokens_in": None,
        "tokens_out": None,
        "token_source": None,
    }


def test_openclaw_execution_result_normalizes_success():
    result = normalize_openclaw_execution_result(
        {
            "status": "completed",
            "output": "changed files",
        },
        backend_id="local_openclaw",
        role="execution",
        duration_seconds=2.5,
    )

    assert result.backend_id == "local_openclaw"
    assert result.role == "execution"
    assert result.success is True
    assert result.exit_reason == "completed"
    assert result.output == "changed files"
    assert result.duration_seconds == 2.5
    assert result.failure_category is None


def test_openclaw_execution_result_classifies_capacity_failure():
    result = normalize_openclaw_execution_result(
        {
            "status": "failed",
            "error": "session file locked",
        },
        backend_id="local_openclaw",
        role="execution",
    )

    assert result.success is False
    assert result.exit_reason == "session file locked"
    assert result.failure_category == "backend_capacity_limit"


def test_openclaw_execution_result_classifies_timeout_with_terminal_reason():
    result = normalize_openclaw_execution_result(
        {
            "status": "failed",
            "error": "execution timed out",
        },
        backend_id="local_openclaw",
        role="execution",
    )

    assert result.success is False
    assert result.failure_category == "backend_timeout"
    assert result.terminal_reason == "timeout_before_backend_completion"


def test_execution_loop_uses_runtime_backend_result_for_execution_path():
    from app.services.orchestration.phases.execution_loop import (
        _normalize_runtime_execution_result,
    )

    class RuntimeWithNormalizer:
        def normalize_execution_result(self, result, *, role, duration_seconds):
            return RuntimeBackendResult(
                backend_id="stub_runtime",
                role=role,
                success=True,
                exit_reason="completed",
                output=result.get("output"),
                duration_seconds=duration_seconds,
            )

    normalized = _normalize_runtime_execution_result(
        RuntimeWithNormalizer(),
        {"status": "completed", "output": "ok"},
        duration_seconds=0.5,
    )

    assert normalized == RuntimeBackendResult(
        backend_id="stub_runtime",
        role="execution",
        success=True,
        exit_reason="completed",
        output="ok",
        duration_seconds=0.5,
    )


def test_execution_loop_persists_runtime_backend_result_metadata(db_session):
    from app.services.orchestration.phases.execution_loop import (
        _persist_runtime_backend_result,
    )

    _, session, task = _make_project_session_task(db_session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.flush()

    _persist_runtime_backend_result(
        db_session,
        execution.id,
        RuntimeBackendResult(
            backend_id="local_openclaw",
            role="execution",
            success=False,
            exit_reason="execution timed out",
            output="timeout",
            duration_seconds=10,
            failure_category="backend_timeout",
            terminal_reason="timeout_before_backend_completion",
        ),
    )

    db_session.refresh(execution)
    assert execution.backend_id == "local_openclaw"
    assert execution.failure_category == "backend_timeout"


def test_worker_and_tasks_endpoint_do_not_import_run_state_attempt_helpers():
    forbidden_names = {
        "mark_task_attempt_pending",
        "mark_task_attempt_running",
        "mark_task_attempt_done",
        "mark_task_attempt_failed",
        "mark_task_attempt_cancelled",
    }
    checked_paths = [
        Path("app/tasks/worker.py"),
        Path("app/api/v1/endpoints/tasks.py"),
    ]

    for path in checked_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported_names = {alias.name for alias in node.names}
            overlap = forbidden_names & imported_names
            assert not overlap, f"{path} imports direct run_state helpers: {overlap}"


def test_stub_backends_are_not_production_registered():
    backend_names = {descriptor.name for descriptor in list_supported_backends()}

    assert "stub_success" not in backend_names
    assert "stub_capacity" not in backend_names


def test_stub_backend_rejected_unless_test_backends_enabled(db_session, monkeypatch):
    from app.services.agents.agent_runtime import create_agent_runtime
    from app.services.agents.agent_backends import UnsupportedAgentBackendError
    from app.services.agents import agent_runtime as runtime_module

    monkeypatch.setattr(runtime_module.settings, "ENABLE_TEST_RUNTIME_BACKENDS", False)
    monkeypatch.setattr(runtime_module.settings, "EXECUTION_BACKEND", "stub_success")

    with pytest.raises(UnsupportedAgentBackendError, match="test-only"):
        create_agent_runtime(db_session, session_id=1, role=BackendRole.EXECUTION)


def test_stub_success_backend_enabled_only_for_tests(db_session, monkeypatch):
    import asyncio

    from app.services.agents.agent_runtime import create_agent_runtime
    from app.services.agents import agent_runtime as runtime_module

    monkeypatch.setattr(runtime_module.settings, "ENABLE_TEST_RUNTIME_BACKENDS", True)
    monkeypatch.setattr(runtime_module.settings, "EXECUTION_BACKEND", "stub_success")

    runtime = create_agent_runtime(db_session, session_id=1, role=BackendRole.EXECUTION)
    raw = asyncio.run(runtime.execute_task("do work"))
    normalized = runtime.normalize_execution_result(raw, role="execution")

    assert raw["status"] == "completed"
    assert normalized.backend_id == "stub_success"
    assert normalized.success is True
    assert normalized.failure_category is None


def test_stub_capacity_backend_reports_capacity_category(db_session, monkeypatch):
    import asyncio

    from app.services.agents.agent_runtime import create_agent_runtime
    from app.services.agents import agent_runtime as runtime_module

    monkeypatch.setattr(runtime_module.settings, "ENABLE_TEST_RUNTIME_BACKENDS", True)
    monkeypatch.setattr(runtime_module.settings, "EXECUTION_BACKEND", "stub_capacity")

    runtime = create_agent_runtime(db_session, session_id=1, role=BackendRole.EXECUTION)
    raw = asyncio.run(runtime.execute_task("do work"))
    normalized = runtime.normalize_execution_result(raw, role="execution")

    assert raw["status"] == "failed"
    assert normalized.backend_id == "stub_capacity"
    assert normalized.success is False
    assert normalized.exit_reason == "backend_at_capacity"
    assert normalized.failure_category == "backend_capacity_limit"
