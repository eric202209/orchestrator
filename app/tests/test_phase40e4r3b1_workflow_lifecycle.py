"""Temporary-SQLite coverage for Phase 40E-4R3B-1 workflow lifecycle binding.

Scenario numbers refer to the R3B-1 brief (section 9). Every test uses an
isolated SQLite file with production pragmas; no provider request is sent.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    WorkflowBudget,
)
from app.services.agents.runtime_configuration import BackendRole
from app.services.execution import workflow_budget as wb
from app.services.execution import workflow_lifecycle as wl
from app.services.execution.workflow_budget import (
    BindingKind,
    BudgetOutcome,
    new_idempotency_key,
    reserve_provider_request,
    resolve_task_execution_workflow,
)
from app.services.orchestration.lifecycle.transitions import (
    revoke_autonomous_continuation,
)
from app.services.session.execution_policy import (
    classify_failure,
    is_celery_retry_exempt_category,
    is_retry_exempt_category,
)
from app.tests.test_phase40e4r3a_workflow_budget import _engine, _raw, _session

MIGRATION_059 = next(m for m in MIGRATIONS if m.version == wb.MIGRATION_VERSION)
APP_ROOT = Path(wl.__file__).resolve().parents[2]


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_ID", "r3b1-test")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_VERSION", "1")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_REQUESTS", 3)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_DURATION_SECONDS", 600)


@pytest.fixture
def r3b(tmp_path: Path):
    path = tmp_path / "r3b1.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_059,))
    db = _session(engine)
    try:
        yield path, engine, db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def r3b_http(r3b):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.router import api_router
    from app.database import get_db
    from app.dependencies import get_current_active_user
    from app.models import User

    _, engine, _ = r3b
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    factory = sessionmaker(autoflush=False, bind=engine)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    user = User(
        id=1,
        email="retention-test@example.invalid",
        hashed_password="unused",
        is_active=True,
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()


def _graph(db, *, status: str = "running"):
    project = Project(name=f"R3B1 Project {uuid.uuid4()}")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"R3B1 Session {uuid.uuid4()}",
        status=status,
        instance_id=str(uuid.uuid4()),
    )
    task = Task(project_id=project.id, title="R3B1 fixture", status=TaskStatus.PENDING)
    db.add_all([session, task])
    db.flush()
    db.add(
        SessionTask(session_id=session.id, task_id=task.id, status=TaskStatus.PENDING)
    )
    db.commit()
    return project, session, task


def _attempt(db, session, task, number: int, status=TaskStatus.PENDING):
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=number,
        status=status,
    )
    db.add(execution)
    db.commit()
    return execution


def _set_status(db, execution, status):
    execution.status = status
    db.commit()


def _count(path: Path, table: str) -> int:
    return _raw(path, f"SELECT COUNT(*) FROM {table}")[0][0]


def _workflow_row(path: Path, workflow_uuid: str):
    return _raw(
        path,
        "SELECT status, deadline_at, closed_at, reserved_requests "
        "FROM workflow_budgets WHERE workflow_uuid = ?",
        (workflow_uuid,),
    )[0]


def _root(db, session, task, number: int = 1):
    execution = _attempt(db, session, task, number)
    assert wl.bind_new_root(db, execution.id) is BudgetOutcome.CREATED
    return execution, resolve_task_execution_workflow(db, execution.id)


def _identity(db, execution, session):
    db.refresh(session)
    return wl.require_dispatch_identity(
        db, task_execution=execution, session_instance_id=session.instance_id
    )


def _reserve(db, workflow, execution):
    return reserve_provider_request(
        db,
        workflow_uuid=workflow.workflow_uuid,
        idempotency_key=new_idempotency_key(),
        stage="planning",
        runtime_role="planning",
        task_execution_id=execution.id,
    )


def _threads(count: int, target):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(index: int) -> None:
        barrier.wait()
        results[index] = target(index)

    workers = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    return results


def _calls_in_order(source_path: Path, function: str, names: tuple[str, ...]):
    """Line numbers of the first call to each name inside one function."""

    tree = ast.parse(source_path.read_text())
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    )
    lines = {}
    for node in ast.walk(target):
        if isinstance(node, ast.Call):
            callee = node.func
            name = getattr(callee, "id", None) or getattr(callee, "attr", None)
            if name in names:
                lines.setdefault(name, []).append(node.lineno)
    return {name: sorted(lines.get(name, [])) for name in names}


# 9.1 TaskExecution creation coverage -----------------------------------------


def test_01_new_execution_creates_root_under_enforced_config(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    assert workflow.status == "OPEN"
    assert workflow.policy_id == "r3b1-test"
    assert workflow.max_requests == 3
    assert workflow.max_duration_seconds == 600
    assert workflow.root_task_execution_id == execution.id
    identity = _identity(db, execution, session)
    assert identity.workflow_uuid == workflow.workflow_uuid
    assert _count(path, "workflow_budgets") == 1


def test_02_retry_inherits_parent_workflow(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    parent, workflow = _root(db, session, task)
    # A Celery retry continues the same attempt: its binding is unchanged.
    assert _identity(db, parent, session).workflow_uuid == workflow.workflow_uuid
    _set_status(db, parent, TaskStatus.FAILED)
    child = _attempt(db, session, task, 2)
    outcome = wl.bind_continuation(
        db, child.id, parent_task_execution_id=parent.id, binding_kind=BindingKind.RETRY
    )
    assert outcome is BudgetOutcome.BOUND
    assert _identity(db, child, session).workflow_uuid == workflow.workflow_uuid


def test_03_recovery_inherits_parent_workflow(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    parent, workflow = _root(db, session, task)
    _set_status(db, parent, TaskStatus.FAILED)
    child = _attempt(db, session, task, 2)
    outcome = wl.bind_continuation(
        db,
        child.id,
        parent_task_execution_id=parent.id,
        binding_kind=BindingKind.RECOVERY,
    )
    assert outcome is BudgetOutcome.BOUND
    binding = wb.resolve_task_execution_binding(db, child.id)
    assert binding.binding_kind == "RECOVERY"
    assert binding.parent_task_execution_id == parent.id
    assert binding.workflow_uuid == workflow.workflow_uuid


def test_04_unrelated_rerun_creates_different_root_only_after_closure(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    first, workflow = _root(db, session, task)
    # While the first run is OPEN a new root is refused, never inherited.
    _set_status(db, first, TaskStatus.FAILED)
    early = _attempt(db, session, task, 2)
    assert wl.bind_new_root(db, early.id) is BudgetOutcome.OPEN_WORKFLOW_EXISTS
    assert wb.resolve_task_execution_binding(db, early.id) is None
    assert wl.close_task_workflow(db, first.id, reason="t") is BudgetOutcome.CLOSED
    _set_status(db, early, TaskStatus.CANCELLED)
    rerun = _attempt(db, session, task, 3)
    assert wl.bind_new_root(db, rerun.id) is BudgetOutcome.CREATED
    second = resolve_task_execution_workflow(db, rerun.id)
    assert second.workflow_uuid != workflow.workflow_uuid
    assert _count(path, "workflow_budgets") == 2


def test_05_duplicate_delivery_does_not_create_duplicate_root(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    assert wl.bind_new_root(db, execution.id) is BudgetOutcome.ALREADY_EXISTS
    assert _identity(db, execution, session) == _identity(db, execution, session)
    assert _count(path, "workflow_budgets") == 1
    assert _count(path, "workflow_task_execution_bindings") == 1


def test_06_invalid_parent_identity_is_rejected(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    parent, _ = _root(db, session, task)
    child = _attempt(db, session, task, 2)
    assert (
        wl.bind_continuation(
            db, child.id, parent_task_execution_id=None, binding_kind=BindingKind.RETRY
        )
        is BudgetOutcome.IDENTITY_CONFLICT
    )
    _, other_session, other_task = _graph(db)
    stranger = _attempt(db, other_session, other_task, 2)
    assert (
        wl.bind_continuation(
            db,
            stranger.id,
            parent_task_execution_id=parent.id,
            binding_kind=BindingKind.RECOVERY,
        )
        is BudgetOutcome.IDENTITY_CONFLICT
    )
    for unbound in (child, stranger):
        assert wb.resolve_task_execution_binding(db, unbound.id) is None


def test_07_session_generation_mismatch_is_rejected(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    parent, _ = _root(db, session, task)
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        wl.require_dispatch_identity(
            db, task_execution=parent, session_instance_id=str(uuid.uuid4())
        )
    assert raised.value.reason == "session_generation_mismatch"
    session.instance_id = str(uuid.uuid4())
    db.commit()
    child = _attempt(db, session, task, 2)
    assert (
        wl.bind_continuation(
            db,
            child.id,
            parent_task_execution_id=parent.id,
            binding_kind=BindingKind.RECOVERY,
        )
        is BudgetOutcome.IDENTITY_CONFLICT
    )


def test_08_attempt_number_mismatch_is_rejected(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    _, session2, task2 = _graph(db)
    later = _attempt(db, session2, task2, 5)
    assert wl.bind_new_root(db, later.id) is BudgetOutcome.CREATED
    earlier = _attempt(db, session2, task2, 4)
    assert (
        wl.bind_continuation(
            db,
            earlier.id,
            parent_task_execution_id=later.id,
            binding_kind=BindingKind.RETRY,
        )
        is BudgetOutcome.IDENTITY_CONFLICT
    )
    # A bound attempt whose row no longer matches its binding is refused.
    execution, _ = _root(db, session, task)
    execution.attempt_number = 9
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, execution, session)
    assert raised.value.reason == "attempt_identity_mismatch"


def test_09_closed_workflow_cannot_be_inherited(r3b, enforced):
    _, _, db = r3b
    _, session, task = _graph(db)
    parent, _ = _root(db, session, task)
    _set_status(db, parent, TaskStatus.DONE)
    assert wl.close_task_workflow(db, parent.id, reason="t") is BudgetOutcome.CLOSED
    child = _attempt(db, session, task, 2)
    assert (
        wl.bind_continuation(
            db,
            child.id,
            parent_task_execution_id=parent.id,
            binding_kind=BindingKind.RECOVERY,
        )
        is BudgetOutcome.WORKFLOW_CLOSED
    )
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, parent, session)
    assert raised.value.reason == "workflow_closed"


def test_expired_workflow_refuses_binding_and_worker_admission(
    r3b, enforced, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    path, _, db = r3b
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: start)
    _, session, task = _graph(db)
    parent, workflow = _root(db, session, task)
    deadline = workflow.deadline_at
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: deadline)

    child = _attempt(db, session, task, 2)
    assert (
        wl.bind_continuation(
            db,
            child.id,
            parent_task_execution_id=parent.id,
            binding_kind=BindingKind.RECOVERY,
        )
        is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    )
    next_root = _attempt(db, session, task, 3)
    assert (
        wl.bind_new_root(db, next_root.id)
        is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    )
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, parent, session)
    assert raised.value.reason == "workflow_deadline_expired"
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "OPEN"


def test_concurrent_binding_samples_the_deadline_after_lock(r3b, enforced, monkeypatch):
    from datetime import datetime, timedelta, timezone

    path, engine, db = r3b
    _clock = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: _clock)
    _, session, task = _graph(db)
    parent, workflow = _root(db, session, task)
    _set_status(db, parent, TaskStatus.FAILED)
    before_deadline = _attempt(db, session, task, 2)
    at_deadline = _attempt(db, session, task, 3)
    parent_execution_id = parent.id
    before_deadline_execution_id = before_deadline.id
    at_deadline_execution_id = at_deadline.id
    workflow_uuid = workflow.workflow_uuid
    instants = {
        "before-deadline": workflow.deadline_at - timedelta(microseconds=1),
        "at-deadline": workflow.deadline_at,
    }
    monkeypatch.setattr(
        wb,
        "_trusted_utc_now",
        lambda: instants[threading.current_thread().name],
    )
    barrier = threading.Barrier(2)
    outcomes = {}

    def bind(execution_id):
        local = _thread_session(engine)
        try:
            barrier.wait()
            outcomes[threading.current_thread().name] = wl.bind_continuation(
                local,
                execution_id,
                parent_task_execution_id=parent_execution_id,
                binding_kind=BindingKind.RETRY,
            )
        finally:
            local.close()

    threads = [
        threading.Thread(
            name="before-deadline", target=bind, args=(before_deadline.id,)
        ),
        threading.Thread(name="at-deadline", target=bind, args=(at_deadline.id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert outcomes["before-deadline"] is BudgetOutcome.BOUND
    assert outcomes["at-deadline"] is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    assert (
        wb.resolve_task_execution_binding(db, before_deadline_execution_id) is not None
    )
    assert wb.resolve_task_execution_binding(db, at_deadline_execution_id) is None
    assert _workflow_row(path, workflow_uuid)[0] == "OPEN"


def test_expired_planning_workflow_is_not_republished(r3b, enforced, monkeypatch):
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from app.services.planning.planning_session_service import PlanningSessionService

    path, _, db = r3b
    project = Project(name=f"Expired planning {uuid.uuid4()}")
    db.add(project)
    db.flush()
    planning = PlanningSession(
        project_id=project.id,
        title="expired",
        prompt="expired",
        status="failed",
    )
    db.add(planning)
    db.commit()
    wl.bind_planning_root(db, planning.id)
    deadline = wb.resolve_planning_session_workflow(db, planning.id).deadline_at
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: deadline)
    dispatcher_calls = []
    service = PlanningSessionService(db)
    monkeypatch.setattr(
        service,
        "planning_dispatcher",
        SimpleNamespace(dispatch=lambda **kwargs: dispatcher_calls.append(kwargs)),
        raising=False,
    )
    before_status = planning.status

    with pytest.raises(HTTPException) as raised:
        service.retry(planning.id)

    assert raised.value.status_code == 409
    assert raised.value.detail["reason"] == "workflow_deadline_expired"
    assert dispatcher_calls == []
    db.expire_all()
    assert db.get(PlanningSession, planning.id).status == before_status
    assert _count(path, "workflow_budgets") == 1


def test_enforced_resume_is_rejected_before_lifecycle_mutation(r3b, enforced):
    from fastapi import HTTPException

    from app.services.session.session_lifecycle_service import (
        resume_session_lifecycle,
    )

    _, _, db = r3b
    _, session, _ = _graph(db, status="paused")
    before = (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(resume_session_lifecycle(db, session.id))
    assert raised.value.status_code == 409
    db.refresh(session)
    assert (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    ) == before


def test_enforced_manual_checkpoint_start_refuses_before_mutation(
    r3b, enforced, monkeypatch, tmp_path
):
    from pathlib import Path

    from fastapi import HTTPException

    from app.config import settings
    from app.services.session.session_lifecycle_service import start_session_lifecycle
    from app.services.workspace.checkpoint_service import CheckpointService

    _, _, db = r3b
    _, session, _ = _graph(db, status="stopped")
    session.execution_mode = "manual"
    db.commit()
    before = (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    )
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir()
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    (checkpoint_root / f"session_{session.id}_autosave.json").write_text(
        '{"context": {"task_id": 1}, "step": 2}'
    )
    legacy_root = Path(CheckpointService.LEGACY_CHECKPOINT_DIR).resolve()
    path_type = type(legacy_root)
    real_exists = path_type.exists
    legacy_probes = []

    def inaccessible_legacy_root(path):
        if path == legacy_root:
            legacy_probes.append(path)
            raise PermissionError("legacy checkpoint root is not readable")
        return real_exists(path)

    monkeypatch.setattr(path_type, "exists", inaccessible_legacy_root)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(start_session_lifecycle(db, session.id))

    assert raised.value.status_code == 409
    assert raised.value.detail == "cross_generation_resume_unsupported"
    assert legacy_probes == []
    db.refresh(session)
    assert (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    ) == before


def test_enforced_manual_start_fails_closed_when_checkpoint_root_is_inaccessible(
    r3b, enforced, monkeypatch, tmp_path
):
    from pathlib import Path

    from fastapi import HTTPException

    from app.config import settings
    from app.services.session.session_lifecycle_service import start_session_lifecycle
    from app.services.workspace.checkpoint_service import CheckpointService

    _, _, db = r3b
    _, session, _ = _graph(db, status="stopped")
    session.execution_mode = "manual"
    db.commit()
    before = (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    )

    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir()
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    legacy_root = Path(CheckpointService.LEGACY_CHECKPOINT_DIR).resolve()
    path_type = type(legacy_root)
    real_exists = path_type.exists

    def inaccessible_legacy_root(path):
        if path == legacy_root:
            raise PermissionError("legacy checkpoint root is not readable")
        return real_exists(path)

    monkeypatch.setattr(path_type, "exists", inaccessible_legacy_root)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(start_session_lifecycle(db, session.id))

    assert raised.value.status_code == 503
    assert raised.value.detail == "workflow_budget_checkpoint_state_unavailable"
    db.refresh(session)
    assert (
        session.instance_id,
        session.status,
        session.is_active,
        session.resumed_at,
    ) == before


def test_enforced_intervention_reply_refuses_before_mutation(r3b, enforced):
    from fastapi import HTTPException

    from app.models import InterventionRequest
    from app.services.session.intervention_service import submit_intervention_reply

    _, _, db = r3b
    project, session, task = _graph(db, status="awaiting_input")
    request = InterventionRequest(
        session_id=session.id,
        task_id=task.id,
        project_id=project.id,
        intervention_type="guidance",
        prompt="Need operator input",
        status="pending",
    )
    db.add(request)
    db.commit()
    before = (request.status, request.operator_reply, request.replied_at)

    with pytest.raises(HTTPException) as raised:
        submit_intervention_reply(
            db,
            intervention_id=request.id,
            operator_reply="continue",
        )

    assert raised.value.status_code == 409
    db.refresh(request)
    assert (request.status, request.operator_reply, request.replied_at) == before


def test_binding_failure_marks_attempt_failed_and_never_publishes(
    r3b, enforced, monkeypatch, tmp_path
):
    from contextlib import nullcontext
    from fastapi import HTTPException

    from app.services.session import session_runtime_service as runtime_service
    from app.tasks import worker

    path, _, db = r3b
    _, session, task = _graph(db, status="pending")
    calls = []
    monkeypatch.setattr(
        runtime_service,
        "project_execution_serialization_admission",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        runtime_service,
        "admit_fresh_logical_execution",
        lambda *_args, **_kwargs: SimpleNamespace(accepted=True),
    )
    monkeypatch.setattr(
        runtime_service,
        "ensure_task_workspace",
        lambda *_args, **_kwargs: {"workspace_path": str(tmp_path)},
    )
    monkeypatch.setattr(
        runtime_service,
        "project_control_state_location",
        lambda path, *_args, **_kwargs: path,
    )
    monkeypatch.setattr(
        runtime_service,
        "append_orchestration_event",
        lambda **_kwargs: {"event_id": "queued-test"},
    )
    monkeypatch.setattr(
        runtime_service,
        "_maybe_compact_checkpoint_before_dispatch",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "publish_task_execution_dispatch",
        lambda *_args, **_kwargs: {
            "state": "refused",
            "reason": "workflow_binding_failed:SCHEMA_UNAVAILABLE",
        },
    )
    monkeypatch.setattr(
        worker.execute_orchestration_task,
        "delay",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(HTTPException) as raised:
        runtime_service.queue_task_for_session(db, session, task.id)

    assert raised.value.status_code == 503
    assert not calls
    execution = db.query(TaskExecution).one()
    assert execution.status is TaskStatus.FAILED
    assert _count(path, "workflow_budgets") == 0


def test_10_legacy_unbound_behavior_is_unchanged(r3b):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution = _attempt(db, session, task, 1)
    assert settings.WORKFLOW_BUDGET_ENFORCEMENT_ENABLED is False
    assert wl.bind_new_root(db, execution.id) is None
    assert wl.close_task_workflow(db, execution.id, reason="t") is None
    assert wl.close_session_workflows(db, session.id, reason="t") == {}
    assert _identity(db, execution, session) is None
    assert _count(path, "workflow_budgets") == 0
    assert wb.budget_enforcement_status(db, task_execution_id=execution.id) == (
        wb.LEGACY_UNENFORCED
    )
    # Disabled hooks never touch the database, so in-memory test databases
    # (on which every R3A read fails closed) keep working unchanged.
    memory = sessionmaker(bind=create_engine("sqlite://"))()
    attempt = SimpleNamespace(id=1, session_id=1, task_id=1, attempt_number=1)
    assert wl.bind_new_root(memory, 1) is None
    assert (
        wl.require_dispatch_identity(
            memory, task_execution=attempt, session_instance_id="x"
        )
        is None
    )
    wl.bind_planning_root(memory, 1)
    assert wl.require_planning_identity(memory, 1, "g") is None


# 9.2 Closure coverage --------------------------------------------------------


def test_11_successful_terminal_completion_closes(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    _set_status(db, execution, TaskStatus.DONE)
    assert wl.close_task_workflow(db, execution.id, reason="ok") is BudgetOutcome.CLOSED
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "CLOSED"


def test_12_failed_terminal_completion_closes(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    _set_status(db, execution, TaskStatus.FAILED)
    assert wl.close_task_workflow(db, execution.id, reason="x") is BudgetOutcome.CLOSED
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "CLOSED"


def test_13_retry_pending_keeps_workflow_open(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    # schedule_continuation resets the recovering attempt to PENDING.
    _set_status(db, execution, TaskStatus.PENDING)
    outcome = wl.close_task_workflow(db, execution.id, reason="x")
    assert outcome is BudgetOutcome.ATTEMPT_ACTIVE
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "OPEN"


def test_14_recovery_pending_keeps_workflow_open(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    parent, workflow = _root(db, session, task)
    _set_status(db, parent, TaskStatus.FAILED)
    child = _attempt(db, session, task, 2)
    wl.bind_continuation(
        db,
        child.id,
        parent_task_execution_id=parent.id,
        binding_kind=BindingKind.RECOVERY,
    )
    assert wl.close_task_workflow(db, parent.id, reason="x") is (
        BudgetOutcome.ATTEMPT_ACTIVE
    )
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "OPEN"


def test_15_completion_repair_pending_keeps_workflow_open(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    # Completion Repair runs inside the parent attempt while it is RUNNING.
    _set_status(db, execution, TaskStatus.RUNNING)
    assert wl.close_task_workflow(db, execution.id, reason="x") is (
        BudgetOutcome.ATTEMPT_ACTIVE
    )
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "OPEN"


def test_16_session_pause_keeps_identity_and_deadline(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    before = _workflow_row(path, workflow.workflow_uuid)
    old_instance = session.instance_id
    revoke_autonomous_continuation(db, session, resulting_status="paused", commit=True)
    assert session.instance_id != old_instance
    after = _workflow_row(path, workflow.workflow_uuid)
    assert after == before and after[0] == "OPEN"
    assert wb.resolve_task_execution_binding(db, execution.id).workflow_uuid == (
        workflow.workflow_uuid
    )


def test_17_session_resume_gets_no_new_root_or_deadline(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    deadline = _workflow_row(path, workflow.workflow_uuid)[1]
    revoke_autonomous_continuation(db, session, resulting_status="paused", commit=True)
    # Resume creates a fresh attempt in the rotated generation.
    resumed = _attempt(db, session, task, 2)
    assert wl.bind_new_root(db, resumed.id) is BudgetOutcome.OPEN_WORKFLOW_EXISTS
    assert (
        wl.bind_continuation(
            db,
            resumed.id,
            parent_task_execution_id=execution.id,
            binding_kind=BindingKind.RECOVERY,
        )
        is BudgetOutcome.IDENTITY_CONFLICT
    )
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, resumed, session)
    assert raised.value.reason == "attempt_unbound"
    assert _count(path, "workflow_budgets") == 1
    assert _workflow_row(path, workflow.workflow_uuid)[1] == deadline


def test_18_session_cancellation_closes_session_workflows(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    revoke_autonomous_continuation(db, session, resulting_status="stopped", commit=True)
    db.refresh(execution)
    assert execution.status == TaskStatus.CANCELLED
    outcomes = wl.close_session_workflows(db, session.id, reason="session_stopped")
    assert outcomes == {workflow.workflow_uuid: BudgetOutcome.CLOSED}
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.WORKFLOW_CLOSED


def test_19_workflow_cancellation_is_durable_without_refund(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED
    _set_status(db, execution, TaskStatus.CANCELLED)
    assert wl.close_task_workflow(db, execution.id, reason="cancelled") is (
        BudgetOutcome.CLOSED
    )
    status, _, closed_at, reserved = _workflow_row(path, workflow.workflow_uuid)
    assert (status, reserved) == ("CLOSED", 1) and closed_at is not None
    assert _count(path, "provider_request_reservations") == 1


def test_20_repeated_closure_is_idempotent(r3b, enforced):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    _set_status(db, execution, TaskStatus.DONE)
    assert wl.close_task_workflow(db, execution.id, reason="a") is BudgetOutcome.CLOSED
    first = _workflow_row(path, workflow.workflow_uuid)
    for _ in range(3):
        assert wl.close_task_workflow(db, execution.id, reason="a") is (
            BudgetOutcome.ALREADY_CLOSED
        )
    assert _workflow_row(path, workflow.workflow_uuid) == first


# 9.3 Identity propagation ----------------------------------------------------


def _dispatch_identity(db):
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    return _identity(db, execution, session), execution, workflow


def test_21_planning_runtime_identity(r3b, enforced):
    _, _, db = r3b
    identity, execution, workflow = _dispatch_identity(db)
    runtime = SimpleNamespace()
    wl.attach_workflow_identity(runtime, identity, BackendRole.PLANNING)
    assert runtime.workflow_identity.role == "planning"
    assert runtime.workflow_identity.task_execution_id == execution.id
    assert runtime.workflow_identity.workflow_uuid == workflow.workflow_uuid
    assert runtime.workflow_identity.workflow_kind == "TASK_EXECUTION"


def test_22_planning_repair_identity(r3b, enforced):
    _, _, db = r3b
    identity, execution, _ = _dispatch_identity(db)
    parent, repair = SimpleNamespace(), SimpleNamespace()
    wl.attach_workflow_identity(parent, identity, BackendRole.PLANNING)
    wl.propagate_workflow_identity(parent, repair, BackendRole.REPAIR)
    assert repair.workflow_identity.role == "repair"
    assert repair.workflow_identity.task_execution_id == execution.id
    assert repair.workflow_identity.session_instance_id == (
        parent.workflow_identity.session_instance_id
    )


def test_23_execution_runtime_identity(r3b, enforced):
    _, _, db = r3b
    identity, execution, _ = _dispatch_identity(db)
    runtime = SimpleNamespace()
    wl.attach_workflow_identity(runtime, identity, BackendRole.EXECUTION)
    assert runtime.workflow_identity.role == "execution"
    assert runtime.workflow_identity.session_id == execution.session_id
    assert runtime.workflow_identity.task_id == execution.task_id


def test_24_debug_runs_on_execution_identity_and_wiring_is_explicit():
    # DEBUG steps run on the execution runtime; only DEBUG repair builds a
    # separate runtime, and both such sites propagate the parent identity.
    sites = (
        (
            APP_ROOT / "services/orchestration/phases/execution_loop.py",
            "DEBUG_REPAIR",
        ),
        (
            APP_ROOT / "services/orchestration/execution/step_support.py",
            "DEBUG_REPAIR",
        ),
        (APP_ROOT / "services/orchestration/planning/planner.py", "REPAIR"),
        (APP_ROOT / "services/orchestration/phases/completion_flow.py", None),
    )
    for path, _role in sites:
        source = path.read_text()
        assert "propagate_workflow_identity(" in source, path
    worker = (APP_ROOT / "tasks/worker.py").read_text()
    assert worker.count("attach_workflow_identity(") == 2
    assert "BackendRole.DEBUG," not in worker


def test_25_debug_repair_identity(r3b, enforced):
    _, _, db = r3b
    identity, execution, _ = _dispatch_identity(db)
    parent, candidate = SimpleNamespace(), SimpleNamespace()
    wl.attach_workflow_identity(parent, identity, BackendRole.EXECUTION)
    wl.propagate_workflow_identity(parent, candidate, BackendRole.DEBUG_REPAIR)
    assert candidate.workflow_identity.role == "debug_repair"
    assert candidate.workflow_identity.task_execution_id == execution.id
    # A runtime reused as its own repair runtime keeps its own role.
    wl.propagate_workflow_identity(parent, parent, BackendRole.DEBUG_REPAIR)
    assert parent.workflow_identity.role == "execution"


def test_26_completion_repair_keeps_parent_attempt(r3b, enforced):
    _, _, db = r3b
    identity, execution, _ = _dispatch_identity(db)
    parent, fast = SimpleNamespace(), SimpleNamespace()
    wl.attach_workflow_identity(parent, identity, BackendRole.EXECUTION)
    wl.propagate_workflow_identity(parent, fast, BackendRole.COMPLETION_REPAIR)
    assert fast.workflow_identity.task_execution_id == execution.id
    assert fast.workflow_identity.workflow_uuid == identity.workflow_uuid
    assert fast.workflow_identity.role == "completion_repair"
    calls = _calls_in_order(
        APP_ROOT / "services/orchestration/phases/completion_flow.py",
        "_attempt_completion_repair",
        ("_create_completion_repair_runtime", "propagate_workflow_identity"),
    )
    assert calls["_create_completion_repair_runtime"][0] < (
        calls["propagate_workflow_identity"][0]
    )


def test_27_completion_summary_uses_owner_attempt_identity(r3b, enforced, monkeypatch):
    from app.services.agents import agent_runtime
    from app.services.orchestration.phases import completion_summary

    _, _, db = r3b
    identity, execution, _ = _dispatch_identity(db)
    created = []

    class _Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            return {"output": "summary"}

    def _factory(db, session_id=None, task_id=None, **kwargs):
        runtime = _Runtime()
        created.append(runtime)
        return runtime

    monkeypatch.setattr(agent_runtime, "create_agent_runtime", _factory)
    owner = SimpleNamespace()
    wl.attach_workflow_identity(owner, identity, BackendRole.EXECUTION)
    result = asyncio.run(
        completion_summary._call_planning_lane("p", db=db, owner_runtime=owner)
    )
    assert result == "summary"
    assert created[0].workflow_identity.task_execution_id == execution.id
    assert created[0].workflow_identity.role == "repair"
    # Without an owner nothing is attached: no arbitrary attempt is chosen.
    asyncio.run(completion_summary._call_planning_lane("p", db=db))
    assert not hasattr(created[1], "workflow_identity")
    source = inspect.getsource(completion_summary._generate_task_summary_with_fallback)
    assert 'owner_runtime=getattr(ctx, "runtime_service", None)' in source


def test_28_interactive_planning_generation_identity(r3b, enforced, monkeypatch):
    from app.services.agents import agent_runtime

    path, _, db = r3b
    project = Project(name=f"R3B1 Planning {uuid.uuid4()}")
    db.add(project)
    db.flush()
    planning = PlanningSession(project_id=project.id, title="p", prompt="p")
    db.add(planning)
    db.commit()
    wl.bind_planning_root(db, planning.id)
    wl.bind_planning_root(db, planning.id)  # idempotent
    assert _count(path, "workflow_budgets") == 1
    identity = wl.require_planning_identity(db, planning.id, planning.generation_id)
    assert identity.workflow_kind == "PLANNING_SESSION"
    assert identity.planning_generation_id == planning.generation_id
    assert identity.task_execution_id is None
    with pytest.raises(wl.WorkflowLifecycleError):
        wl.require_planning_identity(db, planning.id, str(uuid.uuid4()))

    seen = []

    class _Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            seen.append(getattr(self, "workflow_identity", None))
            return {"success": True, "output": "{}"}

    monkeypatch.setattr(
        agent_runtime, "create_agent_runtime", lambda *a, **k: _Runtime()
    )
    with wl.planning_identity_scope(identity):
        agent_runtime.invoke_runtime_prompt(db, "p", role=None)
    agent_runtime.invoke_runtime_prompt(db, "p", role=None)
    assert seen[0].planning_session_id == planning.id
    assert seen[1] is None

    wl.close_planning_workflow(db, planning.id, reason="planning_completed")
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        wl.require_planning_identity(db, planning.id, planning.generation_id)
    assert raised.value.reason == "workflow_closed"


def test_29_missing_enforced_identity_fails_closed(r3b, enforced, monkeypatch):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution = _attempt(db, session, task, 1)
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, execution, session)
    assert raised.value.reason == "attempt_unbound"
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        wl.require_dispatch_identity(
            db, task_execution=None, session_instance_id=session.instance_id
        )
    assert raised.value.reason == "attempt_missing"
    category = classify_failure(
        str(raised.value),
        "backend",
        {"failure_category": raised.value.failure_category},
    )
    assert category == wl.WORKFLOW_IDENTITY_INVALID
    assert is_retry_exempt_category(category)
    assert is_celery_retry_exempt_category(category)
    # Enabled with an invalid policy: no budget is created and dispatch refuses.
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_REQUESTS", 0)
    assert wl.bind_new_root(db, execution.id) is None
    with pytest.raises(wl.WorkflowLifecycleError) as raised:
        _identity(db, execution, session)
    assert raised.value.reason == "policy_invalid"
    assert _count(path, "workflow_budgets") == 0


def test_30_legacy_runtime_construction_remains_compatible(r3b):
    _, _, db = r3b
    _, session, task = _graph(db)
    execution = _attempt(db, session, task, 1)
    runtime = SimpleNamespace()
    identity = _identity(db, execution, session)
    wl.attach_workflow_identity(runtime, identity, BackendRole.EXECUTION)
    wl.propagate_workflow_identity(runtime, SimpleNamespace(), BackendRole.REPAIR)
    assert identity is None and not hasattr(runtime, "workflow_identity")
    assert wl.current_planning_identity() is None


def test_worker_admission_runs_after_claim_and_before_runtime_construction():
    calls = _calls_in_order(
        APP_ROOT / "tasks/worker.py",
        "execute_orchestration_task",
        (
            "_claim_queued_task_for_worker",
            "require_dispatch_identity",
            "create_agent_runtime",
        ),
    )
    claim = calls["_claim_queued_task_for_worker"][0]
    admission = calls["require_dispatch_identity"]
    assert admission
    assert claim < admission[-1] < min(calls["create_agent_runtime"])


@pytest.mark.parametrize(
    ("relative", "function", "hook", "commit_before"),
    [
        (
            "services/session/session_runtime_service.py",
            "queue_task_for_session",
            "bind_new_root",
            True,
        ),
        (
            "services/session/session_runtime_service.py",
            "queue_task_for_session",
            "bind_continuation",
            True,
        ),
        (
            "services/orchestration/lifecycle/completion.py",
            "finalize_success",
            "bind_new_root",
            True,
        ),
        (
            "services/orchestration/lifecycle/completion.py",
            "finalize_success",
            "close_task_workflow",
            True,
        ),
        (
            "api/v1/endpoints/tasks.py",
            "_resume_automatic_chain_after_promotion",
            "_require_root_binding_before_publish",
            True,
        ),
        (
            "api/v1/endpoints/tasks.py",
            "_queue_task_retry",
            "_require_root_binding_before_publish",
            True,
        ),
        (
            "services/session/session_lifecycle_service.py",
            "stop_session_lifecycle",
            "close_session_workflows",
            True,
        ),
    ],
)
def test_creation_and_closure_hooks_run_after_a_commit(
    relative, function, hook, commit_before
):
    calls = _calls_in_order(APP_ROOT / relative, function, ("commit", hook))
    assert calls[hook], (relative, hook)
    assert any(line < calls[hook][0] for line in calls["commit"]) is commit_before


def test_recovery_passes_explicit_parent_attempt():
    source = (
        APP_ROOT / "services/orchestration/coordinators/failure_coordinator.py"
    ).read_text()
    assert "recovery_parent_task_execution_id=getattr(" in source
    queue = (APP_ROOT / "services/session/session_runtime_service.py").read_text()
    assert "parent_task_execution_id=recovery_parent_task_execution_id" in queue


# 9.4 Deletion and retention --------------------------------------------------


def _evidence(db):
    project, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    _reserve(db, workflow, execution)
    _set_status(db, execution, TaskStatus.DONE)
    return project, session, task, execution, workflow


def _one_shot_authorization(db, project, session, task, execution):
    from datetime import datetime, timedelta, timezone

    from app.models import OneShotExecutionAuthorization

    now = datetime.now(timezone.utc)
    digest = "a" * 64
    db.add(
        OneShotExecutionAuthorization(
            authorization_uuid=str(uuid.uuid4()),
            identity_schema_version="one-shot/v1",
            policy_version="1",
            project_id=project.id,
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=task.id,
            task_execution_id=execution.id,
            source_revision_identity="source-revision",
            source_snapshot_digest=digest,
            candidate_uuid=str(uuid.uuid4()),
            candidate_initial_digest=digest,
            execution_profile_id="test-profile",
            execution_profile_version="1",
            operation_scope_id="test-scope",
            operation_scope_version="1",
            operation_scope_digest=digest,
            operator_approval_id="test-approval",
            operator_identity="operator@test.invalid",
            operator_approval_decision="APPROVED",
            operator_approved_at=now,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            identity_sha256=digest,
            state="AUTHORIZED",
            state_changed_at=None,
            revoked_by=None,
            created_at=now,
        )
    )
    db.commit()


def _evidence_counts(path):
    return tuple(
        _count(path, table)
        for table in (
            "workflow_budgets",
            "workflow_task_execution_bindings",
            "provider_request_reservations",
        )
    )


def test_31_task_deletion_without_budget_evidence_succeeds(r3b):
    from app.services.tasks.task_deletion import delete_task_owned_graph

    path, _, db = r3b
    _, session, task = _graph(db)
    _attempt(db, session, task, 1)
    delete_task_owned_graph(db, [task.id])
    db.commit()
    assert _count(path, "task_executions") == 0
    assert _count(path, "tasks") == 0


def test_32_task_deletion_with_budget_evidence_is_restricted(r3b, enforced):
    from app.services.tasks.task_deletion import (
        delete_task_owned_graph,
    )
    from app.services.execution.workflow_lifecycle import (
        RetainedExecutionEvidenceConflict,
    )

    path, _, db = r3b
    _, _, task, execution, _ = _evidence(db)
    before = _evidence_counts(path)
    with pytest.raises(RetainedExecutionEvidenceConflict):
        delete_task_owned_graph(db, [task.id])
        db.commit()
    db.rollback()
    assert _evidence_counts(path) == before
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]
    assert _count(path, "tasks") == 1


def test_33_project_deletion_with_budget_evidence_is_restricted(
    r3b, enforced, monkeypatch
):
    from app.api.v1.endpoints import projects
    from app.services.workspace import checkpoint_service

    path, _, db = r3b
    project, _, _, _, _ = _evidence(db)
    before = _evidence_counts(path)
    monkeypatch.setattr(projects, "get_project_for_user", lambda db, pid, user: project)

    class _Checkpoints:
        def __init__(self, db):
            pass

        def delete_all_checkpoints(self, session_id):
            return 0

        def cleanup_orphaned_checkpoints(self):
            return 0

    monkeypatch.setattr(checkpoint_service, "CheckpointService", _Checkpoints)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        projects.delete_project(project.id, db=db, current_user=None)
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    db.rollback()
    assert _evidence_counts(path) == before
    # The soft-delete half rolled back with it: the request fails as a whole.
    assert _raw(path, "SELECT deleted_at FROM projects") == [(None,)]


def test_34_planning_session_deletion_with_budget_evidence_is_restricted(r3b, enforced):
    from app.services.planning.planning_session_service import PlanningSessionService
    from fastapi import HTTPException

    path, _, db = r3b
    project = Project(name=f"R3B1 Planning {uuid.uuid4()}")
    db.add(project)
    db.flush()
    planning = PlanningSession(project_id=project.id, title="p", prompt="p")
    db.add(planning)
    db.commit()
    wl.bind_planning_root(db, planning.id)
    planning.status = "cancelled"
    db.commit()
    with pytest.raises(HTTPException) as raised:
        PlanningSessionService(db).delete_terminal_session(planning.id)
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    db.rollback()
    assert _count(path, "planning_sessions") == 1
    assert _count(path, "workflow_budgets") == 1


def test_35_session_cleanup_is_soft_and_keeps_evidence(r3b, enforced, monkeypatch):
    from app.api.v1.endpoints import sessions
    from app.services.workspace import checkpoint_service

    path, _, db = r3b
    _, session, _, _, _ = _evidence(db)
    before = _evidence_counts(path)
    monkeypatch.setattr(
        sessions, "_require_session_access", lambda db, sid, user: session
    )

    class _Checkpoints:
        def __init__(self, db):
            pass

        def delete_all_checkpoints(self, session_id):
            return 0

        def cleanup_orphaned_checkpoints(self):
            return 0

    monkeypatch.setattr(checkpoint_service, "CheckpointService", _Checkpoints)
    sessions.delete_session(session.id, db=db, current_user=None)
    assert _raw(path, "SELECT deleted_at IS NOT NULL FROM sessions") == [(1,)]
    assert _evidence_counts(path) == before


def test_36_audit_evidence_rows_cannot_be_deleted_through_the_orm(r3b, enforced):
    path, _, db = r3b
    _evidence(db)
    row = db.query(WorkflowBudget).one()
    db.delete(row)
    with pytest.raises(RuntimeError, match="evidence is retained"):
        db.flush()
    db.rollback()
    assert _evidence_counts(path) == (1, 1, 1)


def test_37_no_orphaned_reservation_records(r3b, enforced):
    from app.services.tasks.task_deletion import delete_task_owned_graph
    from app.services.execution.workflow_lifecycle import (
        RetainedExecutionEvidenceConflict,
    )

    path, _, db = r3b
    _, _, task, _, _ = _evidence(db)
    with pytest.raises(RetainedExecutionEvidenceConflict):
        delete_task_owned_graph(db, [task.id])
    db.rollback()
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    orphans = _raw(
        path,
        "SELECT COUNT(*) FROM provider_request_reservations r "
        "LEFT JOIN workflow_budgets w ON w.workflow_uuid = r.workflow_uuid "
        "LEFT JOIN task_executions t ON t.id = r.task_execution_id "
        "WHERE w.workflow_uuid IS NULL OR "
        "(r.task_execution_id IS NOT NULL AND t.id IS NULL)",
    )
    assert orphans == [(0,)]


def test_38_public_task_deletion_refusal_is_deterministic(r3b, enforced, monkeypatch):
    from app.api.v1.endpoints import tasks
    from fastapi import HTTPException

    path, _, db = r3b
    _, _, task, _, _ = _evidence(db)
    monkeypatch.setattr(tasks, "require_task_read_access", lambda db, tid, user: task)
    before = _evidence_counts(path)
    # The endpoint classifies retained evidence as a stable HTTP conflict.
    for _ in range(2):
        with pytest.raises(HTTPException) as raised:
            tasks.delete_task(task.id, db=db, current_user=None)
        assert raised.value.status_code == 409
        assert raised.value.detail["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
        assert _evidence_counts(path) == before


def test_task_delete_http_409_for_phase39_evidence(r3b, r3b_http, enforced):
    path, _, db = r3b
    project, session, task, execution, _ = _evidence(db)
    _one_shot_authorization(db, project, session, task, execution)

    response = r3b_http.delete(f"/api/v1/tasks/{task.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert _raw(path, "SELECT id FROM tasks") == [(task.id,)]
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]


def test_task_delete_http_409_for_phase40_evidence(r3b, r3b_http, enforced):
    path, _, db = r3b
    _, _, task, execution, _ = _evidence(db)

    response = r3b_http.delete(f"/api/v1/tasks/{task.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert _raw(path, "SELECT id FROM tasks") == [(task.id,)]
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]


def test_project_delete_and_purge_preserve_checkpoint_on_combined_evidence(
    r3b, r3b_http, enforced, monkeypatch, tmp_path
):
    from datetime import datetime, timedelta, timezone

    from app.config import settings

    path, _, db = r3b
    project, session, task, execution, _ = _evidence(db)
    _one_shot_authorization(db, project, session, task, execution)
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir()
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    checkpoint = checkpoint_root / f"session_{session.id}_retained.json"
    checkpoint.write_text('{"retained": true}')

    response = r3b_http.delete(f"/api/v1/projects/{project.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert checkpoint.exists()
    assert _raw(path, "SELECT deleted_at FROM projects") == [(None,)]
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]

    project.deleted_at = datetime.now(timezone.utc) - timedelta(days=365)
    db.commit()
    purge_response = r3b_http.delete("/api/v1/projects/purge-soft-deleted")
    assert purge_response.status_code == 409
    assert purge_response.json()["detail"]["code"] == (
        "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    )
    assert checkpoint.exists()
    assert _raw(path, "SELECT id FROM projects") == [(project.id,)]
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]


def test_planning_delete_http_409_for_retained_workflow(r3b, r3b_http, enforced):
    path, _, db = r3b
    project = Project(name=f"R3B1 Planning HTTP {uuid.uuid4()}")
    db.add(project)
    db.flush()
    planning = PlanningSession(project_id=project.id, title="p", prompt="p")
    db.add(planning)
    db.commit()
    wl.bind_planning_root(db, planning.id)
    planning.status = "cancelled"
    db.commit()

    response = r3b_http.delete(f"/api/v1/planning/sessions/{planning.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert _raw(path, "SELECT id FROM planning_sessions") == [(planning.id,)]
    assert _count(path, "workflow_budgets") == 1


def test_project_purge_integrity_race_rolls_back_before_checkpoint_cleanup(
    r3b, r3b_http, enforced, monkeypatch, tmp_path
):
    from datetime import datetime, timedelta, timezone

    from app.config import settings
    from app.api.v1.endpoints import projects

    path, _, db = r3b
    project, session, task, execution, _ = _evidence(db)
    project.deleted_at = datetime.now(timezone.utc) - timedelta(days=365)
    db.commit()
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir()
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    checkpoint = checkpoint_root / f"session_{session.id}_race.json"
    checkpoint.write_text('{"retained": true}')

    real_exists = projects.retained_execution_evidence_exists
    calls = 0

    def allow_preflight_once(db, **scope):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        return real_exists(db, **scope)

    monkeypatch.setattr(
        projects, "retained_execution_evidence_exists", allow_preflight_once
    )
    response = r3b_http.delete("/api/v1/projects/purge-soft-deleted")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert calls >= 2
    assert checkpoint.exists()
    assert _raw(path, "SELECT id FROM projects") == [(project.id,)]
    assert _raw(path, "SELECT id FROM tasks") == [(task.id,)]
    assert _raw(path, "SELECT id FROM task_executions") == [(execution.id,)]


def test_project_purge_without_evidence_commits_before_file_cleanup(
    r3b, r3b_http, enforced, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    from app.api.v1.endpoints import projects

    path, _, db = r3b
    project, session, _ = _graph(db)
    session_id = session.id
    project.deleted_at = datetime.now(timezone.utc) - timedelta(days=365)
    db.commit()
    cleanup_calls = []

    class _Checkpoints:
        def __init__(self, _db):
            pass

        def delete_all_checkpoints(self, session_id):
            cleanup_calls.append(session_id)
            assert _raw(path, "SELECT id FROM projects") == []
            assert _raw(path, "SELECT id FROM tasks") == []
            return 0

        def checkpoint_artifacts_remaining(self, _session_id):
            return []

        def cleanup_orphaned_checkpoints(self):
            return {}

    monkeypatch.setattr(projects, "CheckpointService", _Checkpoints)
    response = r3b_http.delete("/api/v1/projects/purge-soft-deleted")

    assert response.status_code == 200, response.text
    assert response.json()["purged_count"] == 1
    assert cleanup_calls == [session_id]
    assert _raw(path, "SELECT id FROM sessions") == []
    assert _raw(path, "SELECT id FROM projects") == []
    assert _raw(path, "SELECT id FROM tasks") == []


def test_project_delete_reports_postcommit_checkpoint_cleanup_failure(
    r3b, r3b_http, enforced, monkeypatch, tmp_path
):
    from app.api.v1.endpoints import projects
    from app.config import settings

    path, _, db = r3b
    project, session, _ = _graph(db)
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir()
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    checkpoint = checkpoint_root / f"session_{session.id}_pending.json"
    checkpoint.write_text('{"recoverable": true}')

    class _Checkpoints:
        def __init__(self, _db):
            pass

        def delete_all_checkpoints(self, _session_id):
            raise OSError("simulated filesystem failure")

        def checkpoint_artifacts_remaining(self, _session_id):
            return [str(checkpoint)]

        def cleanup_orphaned_checkpoints(self):
            return {}

    monkeypatch.setattr(projects, "CheckpointService", _Checkpoints)
    response = r3b_http.delete(f"/api/v1/projects/{project.id}")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CHECKPOINT_CLEANUP_PENDING"
    assert str(checkpoint) in response.json()["detail"]["sessions"][str(session.id)]
    assert checkpoint.exists()
    assert _raw(path, "SELECT deleted_at IS NOT NULL FROM projects") == [(1,)]
    assert _raw(path, "SELECT deleted_at IS NOT NULL FROM sessions") == [(1,)]


def test_project_delete_preserves_unrelated_integrity_error(r3b, r3b_http, monkeypatch):
    from app.api.v1.endpoints import projects

    path, _, db = r3b
    project, _, _ = _graph(db)

    def unrelated_integrity_error(*_args, **_kwargs):
        raise IntegrityError("DELETE", {}, RuntimeError("unrelated constraint"))

    monkeypatch.setattr(projects, "delete_task_owned_graph", unrelated_integrity_error)
    response = r3b_http.delete(f"/api/v1/projects/{project.id}")

    assert response.status_code == 500
    assert _raw(path, "SELECT deleted_at FROM projects") == [(None,)]


# 9.5 Concurrency -------------------------------------------------------------


def _thread_session(engine):
    return sessionmaker(autoflush=False, bind=engine)()


def test_39_concurrent_root_creation(r3b, enforced):
    path, engine, db = r3b
    _, session, task = _graph(db)
    execution = _attempt(db, session, task, 1)
    execution_id = execution.id

    def bind(_):
        local = _thread_session(engine)
        try:
            return wl.bind_new_root(local, execution_id)
        finally:
            local.close()

    outcomes = _threads(8, bind)
    assert outcomes.count(BudgetOutcome.CREATED) == 1
    assert outcomes.count(BudgetOutcome.ALREADY_EXISTS) == 7
    # Two different attempts racing for the same Task: one root only.
    _, session2, task2 = _graph(db)
    racers = [_attempt(db, session2, task2, n).id for n in (1, 2)]

    def race(index):
        local = _thread_session(engine)
        try:
            return wl.bind_new_root(local, racers[index])
        finally:
            local.close()

    raced = _threads(2, race)
    assert sorted(o.value for o in raced) == ["CREATED", "OPEN_WORKFLOW_EXISTS"]
    assert _count(path, "workflow_budgets") == 2


def test_40_concurrent_retry_binding(r3b, enforced):
    path, engine, db = r3b
    _, session, task = _graph(db)
    parent, _ = _root(db, session, task)
    _set_status(db, parent, TaskStatus.FAILED)
    parent_id, child_id = parent.id, _attempt(db, session, task, 2).id

    def bind(_):
        local = _thread_session(engine)
        try:
            return wl.bind_continuation(
                local,
                child_id,
                parent_task_execution_id=parent_id,
                binding_kind=BindingKind.RETRY,
            )
        finally:
            local.close()

    outcomes = _threads(8, bind)
    assert outcomes.count(BudgetOutcome.BOUND) == 1
    assert outcomes.count(BudgetOutcome.ALREADY_BOUND) == 7
    assert _count(path, "workflow_task_execution_bindings") == 2


def test_41_concurrent_closure(r3b, enforced):
    path, engine, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    _set_status(db, execution, TaskStatus.DONE)
    execution_id = execution.id

    def close(_):
        local = _thread_session(engine)
        try:
            return wl.close_task_workflow(local, execution_id, reason="race")
        finally:
            local.close()

    outcomes = _threads(8, close)
    assert outcomes.count(BudgetOutcome.CLOSED) == 1
    assert outcomes.count(BudgetOutcome.ALREADY_CLOSED) == 7
    assert _workflow_row(path, workflow.workflow_uuid)[0] == "CLOSED"


def test_42_duplicate_worker_delivery_reads_one_identity(r3b, enforced):
    path, engine, db = r3b
    _, session, task = _graph(db)
    execution, _ = _root(db, session, task)
    execution_id, instance_id = execution.id, session.instance_id
    before = _evidence_counts(path)

    def deliver(_):
        local = _thread_session(engine)
        try:
            attempt = local.get(TaskExecution, execution_id)
            # A redelivery re-runs the creation hook, then Worker admission.
            wl.bind_new_root(local, execution_id)
            return wl.require_dispatch_identity(
                local, task_execution=attempt, session_instance_id=instance_id
            )
        finally:
            local.close()

    identities = _threads(4, deliver)
    assert len(set(identities)) == 1
    assert _evidence_counts(path) == before


def test_43_worker_reconstruction_after_restart(r3b, enforced):
    path, engine, db = r3b
    _, session, task = _graph(db)
    execution, workflow = _root(db, session, task)
    execution_id, instance_id = execution.id, session.instance_id
    db.close()
    engine.dispose()
    restarted = _engine(path)
    local = _thread_session(restarted)
    try:
        attempt = local.get(TaskExecution, execution_id)
        assert wl.bind_new_root(local, execution_id) is BudgetOutcome.ALREADY_EXISTS
        identity = wl.require_dispatch_identity(
            local, task_execution=attempt, session_instance_id=instance_id
        )
        assert identity.workflow_uuid == workflow.workflow_uuid
        assert _count(path, "workflow_budgets") == 1
        assert (
            _workflow_row(path, workflow.workflow_uuid)[1]
            == _raw(path, "SELECT deadline_at FROM workflow_budgets")[0][0]
        )
    finally:
        local.close()
        restarted.dispose()


def test_migration_059_alone_does_not_activate_enforcement(r3b):
    path, _, db = r3b
    _, session, task = _graph(db)
    execution = _attempt(db, session, task, 1)
    assert wl.enforcement_enabled() is False
    assert wl.active_policy(wb.WorkflowKind.TASK_EXECUTION) is None
    assert wb.R2_CANDIDATE_POLICY.enforcement_mode is wb.EnforcementMode.DISABLED
    assert wl.bind_new_root(db, execution.id) is None
    assert _count(path, "workflow_budgets") == 0
