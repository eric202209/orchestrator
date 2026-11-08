"""P41-G2-A: deterministic active-Worker authority reproduction.

Actor A is the real production Worker path (Celery ``.apply`` in a thread).
It is held by a test-side barrier at the first consequential boundary after
the step-boundary authority check: the structured workspace file operation.
Actor B uses the supported operator stop API on a separate SQLite connection.
"""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from celery.result import allow_join_result
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.dependencies import get_current_active_user, get_current_user
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.orchestration.execution import ExecutorService
from app.services.session import session_lifecycle_service
from app.services.session.session_runtime_service import (
    get_session_celery_task_ids,
    queue_task_for_session,
)
from app.tasks import worker
from app.tests.test_p41_product_01_deterministic_e2e import (
    TASK_DESCRIPTION,
    _DeterministicProviderAdapter,
    _read_jsonl,
)

BARRIER_TIMEOUT_SECONDS = 60
WORKER_JOIN_TIMEOUT_SECONDS = 120


class ActiveWorkerAuthorityViolation(AssertionError):
    """A stale, already-admitted Worker performed work after authority loss."""


@pytest.fixture
def db_session_factory(tmp_path):
    """File-backed WAL SQLite so Actor B commits on its own connection.

    Overrides the conftest in-memory StaticPool factory, whose single shared
    connection cannot express two independent transactions.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'p41-g2a-authority.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(bind=engine)
    try:
        yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _gated_call(observed: dict[str, Any]) -> dict[str, Any]:
    return next(call for call in observed["file_op_calls"] if call["gated"])


def _security_check(condition: bool, message: str) -> None:
    if not condition:
        raise ActiveWorkerAuthorityViolation(message)


def _run_scenario(
    *,
    api_app,
    db_session_factory,
    isolated_workspace_root: Path,
    isolated_runtime_root: Path,
    monkeypatch,
    revoke_authority: bool,
    barrier: str = "file_ops_entry",
) -> dict[str, Any]:
    for setting, value in (
        ("INLINE_PLANNING", True),
        ("PLANNING_BACKEND", "openai_chat_completions"),
        ("EXECUTION_BACKEND", "openai_chat_completions"),
        ("PLANNER_MODEL", "qwen-local"),
        ("EXECUTION_MODEL", "qwen-local"),
        ("AGENT_MODEL", "qwen-local"),
        ("PLANNING_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_CONTEXT_TOKENS", 64_000),
        ("DEBUG_REPAIR_CONTEXT_TOKENS", 64_000),
        ("PLANNING_REPAIR_ENABLED", False),
        ("RUNTIME_WORKSPACE_ENABLED", False),
        ("DEMO_MODE", False),
    ):
        monkeypatch.setattr(settings, setting, value)

    workspace = isolated_workspace_root / "p41-g2a-calculator"
    workspace.mkdir(parents=True)
    (workspace / "calculator.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )
    (workspace / "test_calculator.py").write_text(
        "from calculator import value\n\n\n"
        "def test_value():\n"
        "    assert value() == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=p41-g2a@example.invalid",
            "-c",
            "user.name=P41-G2-A",
            "commit",
            "-qm",
            "disposable baseline",
        ],
        cwd=workspace,
        check=True,
    )

    setup_db = db_session_factory()
    project = Project(
        name="P41 G2A disposable calculator",
        description="Temporary active-Worker authority reproduction project",
        workspace_path=str(workspace),
        user_id=1,
    )
    setup_db.add(project)
    setup_db.commit()
    project_id = project.id

    evaluator = User(
        id=1,
        email="eval@local.dev",
        hashed_password="offline-test-only",
        is_active=True,
    )
    api_app.dependency_overrides[get_current_user] = lambda: evaluator
    api_app.dependency_overrides[get_current_active_user] = lambda: evaluator
    client = TestClient(api_app)
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": "Modify calculator.py",
            "description": TASK_DESCRIPTION,
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    session = SessionModel(
        project_id=project_id,
        name="P41 G2A disposable execution",
        description=TASK_DESCRIPTION,
        status="pending",
        execution_mode="manual",
        is_active=False,
        instance_id=f"p41-g2a-{uuid.uuid4()}",
    )
    setup_db.add(session)
    setup_db.commit()
    session_id = session.id
    setup_db.close()

    provider = _DeterministicProviderAdapter()
    monkeypatch.setattr(worker, "create_agent_runtime", lambda *a, **k: provider)
    monkeypatch.setattr(
        session_lifecycle_service, "create_agent_runtime", lambda *a, **k: provider
    )
    monkeypatch.setattr(worker, "get_db_session", db_session_factory)

    # Celery revoke needs a broker; record what the stop path asked to revoke.
    revoke_calls: list[dict[str, Any]] = []

    def record_revoke(db, sid, terminate=True):
        ids = list(get_session_celery_task_ids(db, sid))
        revoke_calls.append({"session_id": sid, "terminate": terminate, "ids": ids})
        return ids

    monkeypatch.setattr(
        session_lifecycle_service, "revoke_session_celery_tasks", record_revoke
    )

    real_worker_task = worker.execute_orchestration_task
    dispatches: list[dict[str, Any]] = []

    def deliver(kwargs: dict[str, Any], task_id: str | None = None):
        celery_id = task_id or f"p41-g2a-{uuid.uuid4()}"
        dispatches.append({"kwargs": dict(kwargs), "celery_task_id": celery_id})
        applied = real_worker_task.apply(kwargs=kwargs, task_id=celery_id)
        with allow_join_result():
            outcome = applied.get(propagate=True, disable_sync_subtasks=False)
        return SimpleNamespace(id=celery_id, result=outcome)

    class InlineWorkerTask:
        @staticmethod
        def delay(**kwargs: Any):
            return deliver(kwargs)

        @staticmethod
        def apply_async(
            *, kwargs: dict[str, Any], task_id: str | None = None, **_options: Any
        ):
            return deliver(kwargs, task_id=task_id)

    monkeypatch.setattr(worker, "execute_orchestration_task", InlineWorkerTask)

    # Test-side barrier at the first structured workspace mutation. It wraps
    # the production call; no production hook exists or is added.
    # ``after_authority_check`` (P41-G2-B race scenario) instead pauses after
    # the production per-op authority check has passed, just before the write.
    real_execute_file_ops = ExecutorService.execute_file_ops
    paused = threading.Event()
    resume = threading.Event()
    file_op_calls: list[dict[str, Any]] = []

    def gated_execute_file_ops(project_dir, ops, **kwargs):
        first = bool(ops) and not any(call["ops"] for call in file_op_calls)
        record = {
            "ops": len(ops or []),
            "gated": first,
            "provider_calls_before": len(provider.calls),
            "calculator_before": (Path(project_dir) / "calculator.py").read_text(
                encoding="utf-8"
            ),
        }
        file_op_calls.append(record)

        def hold() -> None:
            paused.set()
            if not resume.wait(BARRIER_TIMEOUT_SECONDS):
                raise TimeoutError("P41-G2A resume barrier timed out")
            record["provider_calls_at_resume"] = len(provider.calls)

        if first and barrier == "file_ops_entry":
            hold()
        elif first and barrier == "after_authority_check":
            real_check = kwargs["authority_check"]

            def check_then_hold():
                lost = real_check()
                if lost is None and "authority_check_passed" not in record:
                    record["authority_check_passed"] = True
                    hold()
                return lost

            kwargs = {**kwargs, "authority_check": check_then_hold}
        result = real_execute_file_ops(project_dir, ops, **kwargs)
        record["files_changed"] = list(result.get("files_changed") or [])
        record["success"] = bool(result.get("success"))
        return result

    monkeypatch.setattr(
        ExecutorService, "execute_file_ops", staticmethod(gated_execute_file_ops)
    )

    actor_a: dict[str, Any] = {}

    def run_actor_a() -> None:
        db = db_session_factory()
        try:
            session_row = db.get(SessionModel, session_id)
            actor_a["queue_result"] = queue_task_for_session(
                db, session_row, task_id, timeout_seconds=120
            )
        except BaseException as exc:  # surfaced in the main thread
            actor_a["error"] = exc
        finally:
            db.close()
            paused.set()

    thread = threading.Thread(target=run_actor_a, name="p41-g2a-actor-a")
    thread.start()
    observed: dict[str, Any] = {"task_id": task_id, "session_id": session_id}
    try:
        assert paused.wait(BARRIER_TIMEOUT_SECONDS), "Actor A never reached barrier"
        assert "error" not in actor_a, actor_a.get("error")
        assert any(
            call["gated"] for call in file_op_calls
        ), "Actor A finished without reaching the structured file operation"

        check = db_session_factory()
        try:
            execution = (
                check.query(TaskExecution)
                .filter(TaskExecution.task_id == task_id)
                .one()
            )
            observed["task_execution_id"] = execution.id
            observed["paused_state"] = {
                "task": check.get(Task, task_id).status,
                "task_execution": execution.status,
                "dispatch_state": execution.dispatch_state,
                "session": check.get(SessionModel, session_id).status,
            }
        finally:
            check.close()
        observed["provider_calls_at_pause"] = len(provider.calls)

        if revoke_authority:
            stop = client.post(f"/api/v1/sessions/{session_id}/stop")
            assert stop.status_code == 200, stop.text
            observed["stop_response_status"] = stop.json().get("status")
            check = db_session_factory()
            try:
                execution = check.get(TaskExecution, observed["task_execution_id"])
                session_task = (
                    check.query(SessionTask)
                    .filter(
                        SessionTask.session_id == session_id,
                        SessionTask.task_id == task_id,
                    )
                    .one()
                )
                observed["committed_state"] = {
                    "task": check.get(Task, task_id).status,
                    "session_task": session_task.status,
                    "task_execution": execution.status,
                    "dispatch_state": execution.dispatch_state,
                    "session": check.get(SessionModel, session_id).status,
                    "session_is_active": check.get(SessionModel, session_id).is_active,
                }
            finally:
                check.close()
            observed["calculator_after_stop"] = (workspace / "calculator.py").read_text(
                encoding="utf-8"
            )
    finally:
        resume.set()
        thread.join(WORKER_JOIN_TIMEOUT_SECONDS)
        client.close()
    assert not thread.is_alive(), "Actor A did not finish within the bound"
    assert "error" not in actor_a, actor_a.get("error")

    final = db_session_factory()
    try:
        execution = final.get(TaskExecution, observed["task_execution_id"])
        session_task = (
            final.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.task_id == task_id,
            )
            .one()
        )
        observed["final_state"] = {
            "task": final.get(Task, task_id).status,
            "session_task": session_task.status,
            "task_execution": execution.status,
            "dispatch_state": execution.dispatch_state,
            "session": final.get(SessionModel, session_id).status,
        }
        observed["task_count"] = (
            final.query(Task).filter(Task.project_id == project_id).count()
        )
        observed["execution_count"] = (
            final.query(TaskExecution).filter(TaskExecution.task_id == task_id).count()
        )
    finally:
        final.close()

    events_path = next(
        isolated_runtime_root.rglob(f"session_{session_id}_task_{task_id}.jsonl"),
        None,
    )
    events = _read_jsonl(events_path) if events_path else []
    observed["task_completed_events"] = sum(
        1 for event in events if event["event_type"] == "task_completed"
    )
    verification_path = next(
        isolated_runtime_root.rglob(
            f"verification_session_{session_id}_task_{task_id}_"
            f"execution_{observed['task_execution_id']}.jsonl"
        ),
        None,
    )
    observed["verification_records"] = (
        _read_jsonl(verification_path) if verification_path else []
    )
    observed["file_op_calls"] = file_op_calls
    observed["provider_calls_total"] = len(provider.calls)
    observed["dispatches"] = len(dispatches)
    observed["revoke_calls"] = revoke_calls
    observed["worker_result"] = actor_a.get("queue_result")
    observed["calculator_final"] = (workspace / "calculator.py").read_text(
        encoding="utf-8"
    )
    print("P41_G2A_OBSERVED", json.dumps(observed, default=str, sort_keys=True))
    return observed


@pytest.mark.security
def test_g2a_positive_control_worker_with_retained_authority_completes(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
):
    observed = _run_scenario(
        api_app=api_app,
        db_session_factory=db_session_factory,
        isolated_workspace_root=isolated_workspace_root,
        isolated_runtime_root=isolated_runtime_root,
        monkeypatch=monkeypatch,
        revoke_authority=False,
    )
    assert observed["paused_state"]["task"] == TaskStatus.RUNNING
    assert observed["paused_state"]["task_execution"] == TaskStatus.RUNNING
    assert _gated_call(observed)["files_changed"] == ["calculator.py"]
    assert observed["calculator_final"] == "def value():\n    return 2\n"
    assert any(
        record["verification_status"] == "passed"
        and "test_calculator.py" in record["declared_command"]
        for record in observed["verification_records"]
    )
    assert observed["final_state"]["task"] == TaskStatus.DONE
    assert observed["final_state"]["task_execution"] == TaskStatus.DONE
    assert observed["task_completed_events"] == 1
    assert observed["dispatches"] == 1


@pytest.mark.security
def test_g2a_stopped_worker_must_not_mutate_workspace_after_committed_stop(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
):
    observed = _run_scenario(
        api_app=api_app,
        db_session_factory=db_session_factory,
        isolated_workspace_root=isolated_workspace_root,
        isolated_runtime_root=isolated_runtime_root,
        monkeypatch=monkeypatch,
        revoke_authority=True,
    )
    # Admission was legitimate and the pause precedes any mutation.
    assert observed["paused_state"]["task"] == TaskStatus.RUNNING
    assert observed["paused_state"]["task_execution"] == TaskStatus.RUNNING
    assert observed["paused_state"]["session"] == "running"
    assert _gated_call(observed)["calculator_before"] == (
        "def value():\n    return 1\n"
    )
    # Actor B's supported stop committed and is independently visible.
    committed = observed["committed_state"]
    assert observed["stop_response_status"] == "stopped"
    assert committed["session"] == "stopped"
    assert committed["task_execution"] == TaskStatus.CANCELLED
    assert committed["session_task"] == TaskStatus.CANCELLED
    assert committed["task"] == TaskStatus.CANCELLED
    assert observed["calculator_after_stop"] == "def value():\n    return 1\n"

    # Terminal state and success publication (P41-G1 CAS boundary).
    assert observed["final_state"]["task"] == TaskStatus.CANCELLED
    assert observed["final_state"]["task_execution"] == TaskStatus.CANCELLED
    assert observed["task_completed_events"] == 0
    assert observed["dispatches"] == 1
    assert observed["task_count"] == 1

    # Security contract at the consequential boundaries after authority loss.
    resumed = _gated_call(observed)
    _security_check(
        resumed.get("files_changed") == [],
        f"stale Worker applied file ops after stop: {resumed.get('files_changed')}",
    )
    _security_check(
        not observed["verification_records"],
        "stale Worker executed verification commands after stop: "
        f"{[r.get('declared_command') for r in observed['verification_records']]}",
    )
    _security_check(
        observed["provider_calls_total"] == observed["provider_calls_at_pause"],
        "stale Worker invoked the provider after stop",
    )


@pytest.mark.security
@pytest.mark.xfail(
    raises=ActiveWorkerAuthorityViolation,
    strict=True,
    reason=(
        "P41-G2-B residual check-to-write window: the per-op authority check "
        "is a read, not a fence, so a stop that commits after it passes and "
        "before the write is not prevented (needs OWN-F2 Task-wide fencing)."
    ),
)
def test_g2b_stop_committed_between_authority_check_and_file_write(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
):
    observed = _run_scenario(
        api_app=api_app,
        db_session_factory=db_session_factory,
        isolated_workspace_root=isolated_workspace_root,
        isolated_runtime_root=isolated_runtime_root,
        monkeypatch=monkeypatch,
        revoke_authority=True,
        barrier="after_authority_check",
    )
    resumed = _gated_call(observed)
    # The production check ran and passed before the stop.
    assert resumed["authority_check_passed"] is True
    assert observed["paused_state"]["session"] == "running"
    # No Worker lock prevents the stop from committing inside the window.
    assert observed["stop_response_status"] == "stopped"
    assert observed["committed_state"]["task"] == TaskStatus.CANCELLED
    assert observed["calculator_after_stop"] == "def value():\n    return 1\n"

    # Boundary B: the next consequential operation is still refused.
    assert not observed["verification_records"]
    assert observed["provider_calls_total"] == observed["provider_calls_at_pause"]
    assert observed["final_state"]["task"] == TaskStatus.CANCELLED
    assert observed["final_state"]["task_execution"] == TaskStatus.CANCELLED
    assert observed["task_completed_events"] == 0
    assert observed["dispatches"] == 1

    _security_check(
        resumed.get("files_changed") == [],
        "stop committed inside the check-to-write window; op applied: "
        f"{resumed.get('files_changed')}",
    )
