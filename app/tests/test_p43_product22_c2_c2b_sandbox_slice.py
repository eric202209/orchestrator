"""P43-PRODUCT-22-C2-C2B: one authorized TaskExecution → Host Sandbox slice.

Offline: the real HostSocketLauncher runs on a temporary Unix socket with a
fake Docker executor. No Worker, Beat, broker, provider, or Host is touched.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.execution import workflow_lifecycle as workflow
from app.services.orchestration.execution import execution_flow
from app.services.orchestration.execution.execution_flow import assess_step_execution
from app.services.orchestration.execution.sandbox_gateway import (
    SandboxExecutionContext,
    SandboxExecutionGateway,
    SandboxLocalExecutionRefused,
    refuse_local_execution,
)
from app.services.orchestration.phases import execution_local_steps
from app.services.orchestration.phases.execution_local_steps import (
    _execute_local_shell_commands_step,
    _execute_simple_verification_step,
)
from app.services.orchestration.run_state.transitions import (
    TaskCompletionRejected,
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
)
from app.services.sandbox_bridge.authorization import verify_authorization
from app.services.sandbox_bridge.contract import (
    PROFILE,
    ExecutionRequest,
    ExecutionResult,
    RequestRejected,
)
from app.services.sandbox_bridge.transport import (
    HostSocketLauncher,
    SandboxClient,
    _mac,
    _receive,
    _send,
)
from app.tasks.worker_support.dispatch import _claim_queued_task_for_worker

PASS = "python3 -c 'import sys; sys.exit(0)'"
FAIL = "python3 -c 'import sys; sys.exit(3)'"
KEY = b"c2b-test-key-".ljust(32, b"k")
_MIGRATIONS = tuple(
    m
    for m in MIGRATIONS
    if m.version
    in {"059_workflow_provider_request_budget", "060_task_execution_dispatch_contract"}
)


class FakeDocker:
    """Host-side executor double: records every admitted request."""

    def __init__(self, on_execute=None):
        self.calls: list[ExecutionRequest] = []
        self.on_execute = on_execute

    def __call__(self, request, _cancel):
        self.calls.append(request)
        if self.on_execute:
            self.on_execute(request)
        code = ast.literal_eval(
            ast.parse(request.code).body[-1].value.args[0]  # sys.exit(<int>)
        )
        return ExecutionResult(
            request.request_id,
            "SUCCESS" if code == 0 else "EXECUTION_FAILED",
            code,
            "",
            "",
            duration=0.01,
        )


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'c2b.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(engine)
    run_schema_migrations(engine, _MIGRATIONS)
    factory = sessionmaker(autoflush=False, expire_on_commit=False, bind=engine)
    db = factory()
    try:
        yield factory, db
    finally:
        db.close()
        engine.dispose()


def _claimed_attempt(db):
    """Existing lifecycle: configure → publish (fence) → exact Worker claim."""

    project = Project(name=f"c2b-{uuid.uuid4()}")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"c2b-{uuid.uuid4()}",
        status="running",
        is_active=True,
        instance_id=str(uuid.uuid4()),
    )
    task = Task(
        project_id=project.id,
        title="C2B slice",
        description="Run the bounded verification.",
        status=TaskStatus.PENDING,
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.PENDING
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db.add_all([link, execution])
    db.commit()
    assert workflow.configure_task_execution_dispatch(
        db,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind="ROOT",
        parent_task_execution_id=None,
        prompt=task.description,
        timeout_seconds=1800,
        context={"planner_contract": {"schema": "bounded-v1"}},
    )
    db.commit()
    published = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=lambda **_kwargs: None
    )
    assert published["state"] == "published_unclaimed"
    execution = db.query(TaskExecution).filter_by(id=execution.id).one()
    claimed, reason, _, link = _claim_queued_task_for_worker(
        db=db,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=session.instance_id,
        task_execution_id=execution.id,
        celery_task_id=execution.dispatch_celery_task_id,
    )
    assert claimed, reason
    db.expire_all()
    execution = db.query(TaskExecution).filter_by(id=execution.id).one()
    assert execution.dispatch_state == "CLAIMED"
    assert execution.dispatch_lease_fence == 1
    return session, task, link, execution


def _context(db, session, task, execution, **overrides):
    values = dict(
        db=db,
        task_execution_id=execution.id,
        session_id=session.id,
        task_id=task.id,
        session_instance_id=session.instance_id,
    )
    values.update(overrides)
    return SandboxExecutionContext(**values)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Real Launcher admission on an isolated socket, fake Docker behind it."""

    started = []

    def start(executor, *, require_authorization=True, key=KEY):
        socket_dir = tmp_path / f"sock{len(started)}"
        socket_dir.mkdir(mode=0o700)
        path = socket_dir / "launcher.sock"
        launcher = HostSocketLauncher(
            path,
            KEY,
            os.geteuid(),
            executor,
            tmp_path / f"ledger{len(started)}" / "ledger.sqlite3",
            require_authorization=require_authorization,
        )
        launcher.started_at -= 60
        stop = threading.Event()
        thread = threading.Thread(target=launcher.serve_forever, args=(stop,))
        thread.start()
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        started.append((stop, thread))
        key_file = tmp_path / f"bridge{len(started)}.key"
        key_file.write_text(key.hex())
        key_file.chmod(0o600)
        monkeypatch.setenv("SANDBOX_BRIDGE_SOCKET", str(path))
        monkeypatch.setenv("SANDBOX_BRIDGE_KEY_FILE", str(key_file))
        return path

    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "c2b-sandbox")
    yield start
    for stop, thread in started:
        stop.set()
        thread.join(5)


@pytest.fixture
def no_local_execution(monkeypatch):
    """Any local subprocess from the verification route is a test failure."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("local execution fallback attempted")

    monkeypatch.setattr(execution_flow.subprocess, "run", forbidden)
    monkeypatch.setattr(execution_flow, "_execute_portable_posix_command", forbidden)
    monkeypatch.setattr(execution_local_steps.subprocess, "run", forbidden)


def _run_step(tmp_path, context, command=PASS):
    project_dir = tmp_path / "workspace"
    project_dir.mkdir(exist_ok=True)
    return _execute_simple_verification_step(
        project_dir=project_dir,
        commands=[command],
        verification_command="",
        sandbox_context=context,
    )


# --- Vertical slice ---------------------------------------------------------


def test_claimed_attempt_executes_through_host_and_completes(
    lifecycle, host, tmp_path, no_local_execution
):
    _, db = lifecycle
    session, task, link, execution = _claimed_attempt(db)
    docker = FakeDocker()
    host(docker)

    step = {"commands": [PASS], "verification": "", "expected_files": []}
    step_result = _run_step(tmp_path, _context(db, session, task, execution))

    assert step_result["status"] == "completed", step_result
    assert len(docker.calls) == 1
    admitted = docker.calls[0]
    assert admitted.code == "import sys; sys.exit(0)"
    assert admitted.version == 2
    auth = admitted.authorization
    assert auth["task_execution_id"] == execution.id
    assert auth["attempt_number"] == 1
    assert auth["dispatch_fence"] == 1
    assert auth["dispatch_celery_task_id"] == execution.dispatch_celery_task_id
    assert auth["session_instance_id"] == session.instance_id
    assert auth["worker_identity"].split(":")[1] == str(os.getpid())
    assert auth["nonce"] == admitted.request_id
    evidence = step_result["_verification_evidence"][0]
    assert evidence["execution_kind"] == "host_sandbox"
    assert evidence["verification_status"] == "passed"

    # Existing result handling: assessment then conditional completion.
    assessment = assess_step_execution(
        db=db,
        session_id=session.id,
        task_id=task.id,
        project_dir=tmp_path / "workspace",
        step=step,
        step_result=step_result,
        step_started_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        validation_profile="test_only",
    )
    assert assessment.step_status == "success", assessment.error_message
    task_row = db.query(Task).filter_by(id=task.id).one()
    execution_row = db.query(TaskExecution).filter_by(id=execution.id).one()
    mark_task_attempt_done(
        task=task_row, session_task_link=link, task_execution=execution_row
    )
    db.commit()
    db.expire_all()
    assert db.query(TaskExecution).filter_by(id=execution.id).one().status is (
        TaskStatus.DONE
    )


def test_sandbox_nonzero_exit_is_a_failed_step_not_a_fallback(
    lifecycle, host, tmp_path, no_local_execution
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    host(docker)
    result = _run_step(tmp_path, _context(db, session, task, execution), FAIL)
    assert result["status"] == "failed"
    assert len(docker.calls) == 1
    assert result["_verification_evidence"][0]["process_exit_code"] == 3


# --- Worker authority negatives (never reach the Host) ----------------------


def _mutate(db, execution_id, **values):
    db.query(TaskExecution).filter_by(id=execution_id).update(
        values, synchronize_session=False
    )
    db.commit()


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ("missing", "task_execution_missing"),
        ("forged_session", "task_execution_binding_mismatch"),
        ("no_context", "trusted_execution_context_missing"),
        ("generation", "session_generation_mismatch"),
        ("worker", "worker_ownership_mismatch"),
        ("fence", "dispatch_not_claimed"),
        ("newer_attempt", "stale_attempt"),
        ("cancelled", "task_execution_cancelled"),
        ("terminal", "task_execution_not_running"),
        ("session_stopped", "session_not_running"),
    ],
)
def test_invalid_authority_never_reaches_host(
    lifecycle, host, tmp_path, no_local_execution, mutation, reason
):
    _, db = lifecycle
    session, task, link, execution = _claimed_attempt(db)
    docker = FakeDocker()
    host(docker)
    context = _context(db, session, task, execution)
    if mutation == "missing":
        context = _context(db, session, task, execution, task_execution_id=999999)
    elif mutation == "forged_session":
        context = _context(db, session, task, execution, session_id=session.id + 1)
    elif mutation == "no_context":
        context = None
    elif mutation == "generation":
        context = _context(
            db, session, task, execution, session_instance_id=str(uuid.uuid4())
        )
    elif mutation == "worker":
        _mutate(db, execution.id, worker_pid=os.getpid() + 100000)
    elif mutation == "fence":
        _mutate(db, execution.id, dispatch_state="READY", dispatch_lease_fence=2)
    elif mutation == "newer_attempt":
        db.add(
            TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=2,
                status=TaskStatus.PENDING,
            )
        )
        db.commit()
    elif mutation == "cancelled":
        mark_task_attempt_cancelled(
            task=db.query(Task).filter_by(id=task.id).one(),
            session_task_link=link,
            task_execution=db.query(TaskExecution).filter_by(id=execution.id).one(),
        )
        db.commit()
    elif mutation == "terminal":
        _mutate(db, execution.id, status=TaskStatus.DONE)
    elif mutation == "session_stopped":
        db.query(SessionModel).filter_by(id=session.id).update({"status": "stopped"})
        db.commit()

    result = _run_step(tmp_path, context)

    assert result["status"] == "failed"
    assert reason in result["output"]
    assert docker.calls == []


# --- Host admission negatives (real Launcher verification) ------------------


def _authorized_request(
    db, session, task, execution, *, clock=time.time, path=None, epoch=None
):
    """Capture the exact request the Gateway would authenticate.

    The epoch is read from the running Launcher over the authenticated socket
    (C2-C3A), exactly as the Gateway does, unless a test supplies one.
    """

    captured = []
    if epoch is None:
        epoch = SandboxClient(path, KEY).launcher_epoch()

    class Capture:
        def launcher_epoch(self):
            return epoch

        def execute(self, request):
            captured.append(request)
            return ExecutionResult.unavailable(request.request_id)

    SandboxExecutionGateway(Capture(), clock=clock).execute_python(
        _context(db, session, task, execution), "import sys; sys.exit(0)", 30
    )
    return captured[0]


def _raw(path, wire, key=KEY):
    payload = json.dumps(wire, separators=(",", ":"), sort_keys=True).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(path))
        _send(
            sock, {"operation": "execute", "request": wire, "mac": _mac(key, payload)}
        )
        return _receive(sock)


def test_operation_bytes_modified_after_authorization_are_rejected(lifecycle, host):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    path = host(docker)
    wire = _authorized_request(db, session, task, execution, path=path).to_wire()
    wire["code"] = "import sys; sys.exit(1)"

    # Re-authenticated with the real key: only the binding catches it.
    assert _raw(path, wire)["error_code"] == "operation_digest_mismatch"
    # Not re-authenticated: HMAC catches it.
    original = _authorized_request(db, session, task, execution, path=path).to_wire()
    payload = json.dumps(original, separators=(",", ":"), sort_keys=True).encode()
    tampered = dict(original, code="import sys; sys.exit(1)")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(path))
        _send(
            sock,
            {"operation": "execute", "request": tampered, "mac": _mac(KEY, payload)},
        )
        assert _receive(sock)["error_code"] == "invalid_authentication"
    # Rebinding the operation to another attempt also fails the identity check.
    rebound = _authorized_request(db, session, task, execution, path=path).to_wire()
    rebound["execution_id"] = f"te-{execution.id}-a2"
    assert _raw(path, rebound)["error_code"] == "authorization_identity_mismatch"
    assert docker.calls == []


def test_invalid_authentication_expiry_replay_and_v1_are_rejected(
    lifecycle, host, tmp_path, no_local_execution, monkeypatch
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    path = host(docker)

    # Expired: contract age window (30 s) still open, binding TTL (20 s) closed.
    stale = _authorized_request(
        db, session, task, execution, clock=lambda: time.time() - 25, path=path
    )
    assert _raw(path, stale.to_wire())["error_code"] == "authorization_expired"

    # v1 request (Task/Session identifiers only) is not a credential.
    v1 = ExecutionRequest(
        str(uuid.uuid4()),
        str(task.id),
        f"te-{execution.id}-a1",
        str(uuid.uuid4()),
        PROFILE,
        "import sys; sys.exit(0)",
        30,
        int(time.time()),
        "a" * 64,
    )
    assert _raw(path, v1.to_wire())["error_code"] == "authorization_required"

    # Replay: the nonce is the ledger request_id; second delivery is refused.
    good = _authorized_request(db, session, task, execution, path=path).to_wire()
    assert _raw(path, good)["execution_status"] == "SUCCESS"
    assert _raw(path, good)["error_code"] == "duplicate_request"
    assert len(docker.calls) == 1

    # Wrong key through the integrated step path: Host rejects, no fallback.
    key_file = tmp_path / "wrong.key"
    key_file.write_text((b"w" * 32).hex())
    key_file.chmod(0o600)
    monkeypatch.setenv("SANDBOX_BRIDGE_KEY_FILE", str(key_file))
    result = _run_step(tmp_path, _context(db, session, task, execution))
    assert result["status"] == "failed"
    # C2-C3A: the wrong key now fails at the authenticated epoch read, before
    # any execute request is built. Rejected, no fallback.
    assert "host_rejected:epoch_reply_invalid" in result["output"]
    assert len(docker.calls) == 1


def test_host_rejection_from_executor_admission_is_not_retried_locally(
    lifecycle, host, tmp_path, no_local_execution
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    calls = []

    def busy(request, _cancel):
        calls.append(request)
        return ExecutionResult(
            request.request_id, "REJECTED", None, "", "", error_code="launcher_busy"
        )

    host(busy)
    result = _run_step(tmp_path, _context(db, session, task, execution))
    assert result["status"] == "failed"
    assert "host_rejected:launcher_busy" in result["output"]
    assert len(calls) == 1


def test_host_unavailable_never_falls_back_to_local_execution(
    lifecycle, host, tmp_path, no_local_execution, monkeypatch
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    host(FakeDocker())
    monkeypatch.setenv("SANDBOX_BRIDGE_SOCKET", str(tmp_path / "absent.sock"))
    result = _run_step(tmp_path, _context(db, session, task, execution))
    assert result["status"] == "failed"
    assert "host_unavailable" in result["output"]


def test_transport_timeout_never_falls_back_to_local_execution(
    lifecycle, host, tmp_path, no_local_execution, monkeypatch
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    host(FakeDocker())
    socket_dir = tmp_path / "silent"
    socket_dir.mkdir(mode=0o700)
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.bind(str(socket_dir / "s.sock"))
    silent.listen(1)
    monkeypatch.setenv("SANDBOX_BRIDGE_SOCKET", str(socket_dir / "s.sock"))
    original = SandboxClient.execute

    def short_timeout(self, request):
        # Shrinks only the response wait; the client code path is unchanged.
        real = socket.socket.settimeout
        monkeypatch.setattr(
            socket.socket, "settimeout", lambda s, v: real(s, min(v, 0.5))
        )
        try:
            return original(self, request)
        finally:
            monkeypatch.setattr(socket.socket, "settimeout", real)

    monkeypatch.setattr(SandboxClient, "execute", short_timeout)
    # Exercise the execute wait (the epoch read has its own bounded wait).
    monkeypatch.setattr(SandboxClient, "launcher_epoch", lambda self: 1)
    try:
        result = _run_step(tmp_path, _context(db, session, task, execution))
    finally:
        silent.close()
    assert result["status"] == "failed"
    assert "host_unavailable" in result["output"]


# --- Late results -----------------------------------------------------------


@pytest.mark.parametrize("change", ["cancel", "newer_attempt", "refenced"])
def test_result_after_cancellation_or_newer_attempt_cannot_commit_success(
    lifecycle, host, tmp_path, no_local_execution, change
):
    factory, db = lifecycle
    session, task, link, execution = _claimed_attempt(db)

    def concurrent_change(_request):
        other = factory()
        try:
            if change == "cancel":
                mark_task_attempt_cancelled(
                    task=other.query(Task).filter_by(id=task.id).one(),
                    session_task_link=other.query(SessionTask)
                    .filter_by(id=link.id)
                    .one(),
                    task_execution=other.query(TaskExecution)
                    .filter_by(id=execution.id)
                    .one(),
                )
            elif change == "newer_attempt":
                other.add(
                    TaskExecution(
                        session_id=session.id,
                        task_id=task.id,
                        attempt_number=2,
                        status=TaskStatus.PENDING,
                    )
                )
            else:
                other.query(TaskExecution).filter_by(id=execution.id).update(
                    {"dispatch_lease_fence": 7}, synchronize_session=False
                )
            other.commit()
        finally:
            other.close()

    docker = FakeDocker(on_execute=concurrent_change)
    host(docker)
    stale_task = db.query(Task).filter_by(id=task.id).one()
    stale_execution = db.query(TaskExecution).filter_by(id=execution.id).one()
    result = _run_step(tmp_path, _context(db, session, task, execution))

    assert len(docker.calls) == 1  # the Host did execute
    assert result["status"] == "failed"
    assert "late_result:" in result["output"]
    if change == "cancel":
        # The existing conditional completion refuses to overwrite cancellation.
        with pytest.raises(TaskCompletionRejected):
            mark_task_attempt_done(
                task=stale_task,
                session_task_link=link,
                task_execution=stale_execution,
            )
        db.rollback()
        db.expire_all()
        assert db.query(TaskExecution).filter_by(id=execution.id).one().status is (
            TaskStatus.CANCELLED
        )


# --- Gating and fail-closed routes ------------------------------------------


def test_unsupported_operations_fail_closed_in_c2_mode(
    lifecycle, host, tmp_path, no_local_execution
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    host(docker)
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    (project_dir / "a.txt").write_text("x")
    # Workspace-reading verification is admitted by the step policy but needs
    # workspace staging, which this slice does not implement.
    reading = "python3 -c \"import pathlib; print(pathlib.Path('a.txt').read_text())\""
    result = execution_flow.execute_verification_command(
        project_dir=project_dir,
        command=reading,
        sandbox_context=_context(db, session, task, execution),
    )
    assert result["success"] is False
    assert result["sandbox_rejection"] == "unsupported_operation"
    with pytest.raises(SandboxLocalExecutionRefused):
        _execute_local_shell_commands_step(
            project_dir=project_dir,
            commands=["mkdir build"],
            verification_command="",
        )
    for route in ("structured_file_ops", "execution_runtime_dispatch"):
        with pytest.raises(SandboxLocalExecutionRefused):
            refuse_local_execution(route)
    from app.services.orchestration.execution.executor import ExecutorService

    with pytest.raises(SandboxLocalExecutionRefused):
        ExecutorService.execute_file_ops(
            project_dir, [{"op": "write_file", "path": "b.txt", "content": "y"}]
        )
    assert not (project_dir / "b.txt").exists()
    assert docker.calls == []


def test_gateway_is_inert_outside_c2_mode(monkeypatch):
    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "disabled")
    refuse_local_execution("structured_file_ops")  # no raise
    monkeypatch.delenv("WORKER_ISOLATION_MODE")
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "c2b-sandbox")
    refuse_local_execution("structured_file_ops")  # no raise


def test_v1_wire_shape_is_unchanged_and_binding_is_hmac_covered():
    request = ExecutionRequest(
        str(uuid.uuid4()),
        "t",
        "e",
        str(uuid.uuid4()),
        PROFILE,
        "print(1)",
        5,
        int(time.time()),
        "b" * 64,
    )
    assert "authorization" not in request.to_wire()
    with pytest.raises(RequestRejected, match="invalid_authorization_presence"):
        ExecutionRequest(*list(request.to_wire().values())[:9], version=2).validate()
    with pytest.raises(RequestRejected, match="invalid_authorization_shape"):
        verify_authorization(
            ExecutionRequest(
                request.request_id,
                "t",
                "e",
                request.workspace_ref,
                PROFILE,
                "print(1)",
                5,
                request.issued_at,
                "b" * 64,
                version=2,
                authorization={"version": 1},
            )
        )
