"""Process-level Supervisor protocol tests with a fake Podman CLI.

Every test talks to a real ``python -m app.supervisor.server`` child process
over its Unix domain socket. The fake runtime checks process, ledger, protocol,
and recovery behavior only; OCI isolation has separate runtime integration
coverage.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import run_schema_migrations
from app.models import Base
from app.services.execution.one_shot_claim import (
    ClaimOutcome,
    claim_one_shot_authorization,
)
from app.services.execution.one_shot_dispatch import (
    AttemptStatus,
    DispatchMode,
    DispatchOutcome,
    ExecutorStartRequest,
    OneShotDispatchRequest,
    SupervisorAdmissionStatus,
    SupervisorTransportError,
    dispatch_one_shot_intent,
)
from app.services.execution.one_shot_supervisor_client import UnixSocketSupervisor
from app.supervisor.ledger import AttemptLedger
from app.supervisor.oci_executor import SUPPORTED_SCOPE_SHA256
from app.tests.test_phase39_f1a_authorization import (
    CANDIDATE_FIXTURE_BYTES,
    SOURCE_SNAPSHOT_FIXTURE_BYTES,
)
from app.tests.test_phase39_f1b_atomic_claim import (
    MIGRATION_057,
    MIGRATION_058,
    _engine,
    _issue,
)
from app.tests.test_phase39_f1c1_dispatch_boundary import (
    PRODUCTION_POSTURE,
    VERIFIED,
    _importers,
    _snapshot_rows,
)

REPO = Path(__file__).resolve().parents[2]
UID = os.geteuid()
IMAGE = "localhost/orchestrator-test@sha256:" + hashlib.sha256(b"image").hexdigest()
CANDIDATE_BYTES = CANDIDATE_FIXTURE_BYTES
SNAPSHOT_BYTES = SOURCE_SNAPSHOT_FIXTURE_BYTES
CANDIDATE_DIGEST = hashlib.sha256(CANDIDATE_BYTES).hexdigest()
SNAPSHOT_DIGEST = hashlib.sha256(SNAPSHOT_BYTES).hexdigest()

_FAKE_PODMAN = r"""#!/usr/bin/env python3
import hashlib, json, pathlib, sys, time
root = pathlib.Path(__file__).resolve().parent
state_path = root / "fake-runtime-state.json"
delay_path = root / "fake-runtime-delay"
exit_path = root / "fake-runtime-exit"
try:
    state = json.loads(state_path.read_text())
except (FileNotFoundError, ValueError):
    state = {"containers": {}}
args = sys.argv[1:]
def save():
    state_path.write_text(json.dumps(state))
def refresh(container):
    if container["State"]["Status"] == "running" and time.time() - container["started"] >= container["delay"]:
        container["State"].update(Status="exited", ExitCode=container["exit_code"], FinishedAt="now")
        save()
if args[:1] == ["info"]:
    print("{}")
elif args[:2] == ["image", "exists"]:
    pass
elif args[:1] == ["create"]:
    name = args[args.index("--name") + 1]
    labels = {}
    for i, value in enumerate(args[:-1]):
        if value == "--label":
            key, label = args[i + 1].split("=", 1)
            labels[key] = label
    identifier = hashlib.sha256(name.encode()).hexdigest()
    state["containers"][name] = {
        "Id": identifier, "Name": name, "Config": {"Labels": labels},
        "State": {"Status": "created", "ExitCode": None},
        "started": None, "delay": 0, "exit_code": 0,
    }
    save()
    print(identifier)
elif args[:1] == ["start"]:
    name = args[-1]
    container = state["containers"][name]
    container["started"] = time.time()
    container["delay"] = float(delay_path.read_text()) if delay_path.exists() else 0
    container["exit_code"] = int(exit_path.read_text()) if exit_path.exists() else 0
    container["State"].update(Status="running", StartedAt=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    refresh(container)
    save()
elif args[:1] == ["inspect"]:
    target = args[-1]
    container = next((value for key, value in state["containers"].items() if key == target or value["Id"].startswith(target)), None)
    if container is None:
        sys.exit(1)
    refresh(container)
    print(json.dumps([container]))
elif args[:1] in (["stop"], ["kill"]):
    name = args[-1]
    container = state["containers"].get(name)
    if container:
        container["State"].update(Status="exited", ExitCode=143, FinishedAt="now")
        save()
elif args[:1] == ["rm"]:
    name = args[-1]
    state["containers"].pop(name, None)
    save()
elif args[:1] == ["cp"]:
    container_path, destination = args[-2:]
    stream = "stderr" if container_path.endswith("stderr") else "stdout"
    pathlib.Path(destination).write_bytes(("fake " + stream).encode())
elif args[:2] == ["ps", "--all"]:
    for container in state["containers"].values():
        if container["Config"]["Labels"].get("io.orchestrator.supervisor") == "phase39-f3/v1":
            print(container["Id"])
else:
    sys.exit(2)
"""


class SupervisorProcess:
    def __init__(self, root: Path, *, allowed_uid=UID):
        self.root = root
        self.state_dir = root / "state"
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        self.socket_path = root / "s.sock"
        self.content_store_dir = root / "content"
        self.content_store_dir.mkdir(mode=0o755, exist_ok=True)
        self.runtime_path = root / "fake-podman"
        self._store_blob(CANDIDATE_DIGEST, CANDIDATE_BYTES)
        self._store_blob(SNAPSHOT_DIGEST, SNAPSHOT_BYTES)
        self.runtime_path.write_text(_FAKE_PODMAN, encoding="utf-8")
        self.runtime_path.chmod(0o755)
        self.allowed_uid = allowed_uid
        self.proc: subprocess.Popen | None = None

    def _store_blob(self, digest: str, content: bytes) -> None:
        path = self.content_store_dir / "sha256" / digest[:2] / digest
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o444)

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "attempts.sqlite3"

    def spawn(self, *, noop_seconds=0.0) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
        (self.root / "fake-runtime-delay").write_text(str(noop_seconds))
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.supervisor.server",
                "--socket",
                str(self.socket_path),
                "--state-dir",
                str(self.state_dir),
                "--allowed-uid",
                str(self.allowed_uid),
                "--content-store-dir",
                str(self.content_store_dir),
                "--image",
                IMAGE,
                "--runtime",
                str(self.runtime_path),
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start(self, *, noop_seconds=0.0) -> "SupervisorProcess":
        assert self.proc is None or self.proc.poll() is not None
        self.proc = self.spawn(noop_seconds=noop_seconds)
        ready, _, _ = select.select([self.proc.stdout], [], [], 20)
        line = self.proc.stdout.readline() if ready else ""
        assert line.startswith("SUPERVISOR_READY"), self.proc.stderr.read()
        return self

    def stop(self) -> int:
        self.proc.terminate()
        return self.proc.wait(timeout=10)

    def crash(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)

    def client(self, **kwargs) -> UnixSocketSupervisor:
        kwargs.setdefault("supervisor_uid", UID)
        return UnixSocketSupervisor(self.socket_path, **kwargs)

    def rows(self):
        with sqlite3.connect(self.ledger_path) as raw:
            return raw.execute(
                "SELECT attempt_idempotency_key, supervisor_attempt_id, status, "
                "start_request_sha256 FROM supervisor_attempts ORDER BY 1"
            ).fetchall()


@pytest.fixture
def supervisor_root():
    # Short path: Unix socket paths are limited to 108 bytes.
    root = Path(tempfile.mkdtemp(prefix="f2-"))
    os.chmod(root, 0o750)
    processes = []
    yield root, processes
    for process in processes:
        if process.proc is not None and process.proc.poll() is None:
            process.proc.kill()
            process.proc.wait(timeout=10)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def supervisor(supervisor_root):
    root, processes = supervisor_root
    process = SupervisorProcess(root)
    processes.append(process)
    return process.start()


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _start_request(seed: str = "a", **changes) -> ExecutorStartRequest:
    request = ExecutorStartRequest(
        schema_version="executor-start-attempt/v1",
        attempt_idempotency_key=_hex(f"key-{seed}"),
        request_payload_sha256=_hex(f"payload-{seed}"),
        authorization_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"auth-{seed}")),
        authorization_identity_sha256=_hex(f"identity-{seed}"),
        claim_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"claim-{seed}")),
        project_id=1,
        session_id=2,
        session_instance_id="instance-1",
        task_id=3,
        task_execution_id=4,
        source_revision_identity="git:" + "0" * 40,
        source_snapshot_digest=SNAPSHOT_DIGEST,
        candidate_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, "candidate")),
        candidate_initial_digest=CANDIDATE_DIGEST,
        execution_profile_id="executor-contained-planning",
        execution_profile_version="v1",
        operation_scope_id="restricted-python-workspace",
        operation_scope_version="1",
        operation_scope_digest=SUPPORTED_SCOPE_SHA256,
    )
    return replace(request, **changes)


def _wait_status(client, key, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        receipt = client.get_attempt_status(key)
        if receipt.status in statuses or time.monotonic() > deadline:
            return receipt
        time.sleep(0.05)


def _raw(socket_path: Path, payload: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(socket_path))
        try:
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        data = b""
        while chunk := connection.recv(65536):
            data += chunk
    return json.loads(data)


# 1-2. Admission and one durable attempt ---------------------------------------


def test_valid_request_is_admitted_and_registered_once(supervisor):
    client = supervisor.client()
    admission = client.admission()
    assert admission.status is SupervisorAdmissionStatus.TRUSTED
    assert admission.durable_deduplication is True

    request = _start_request()
    key = request.attempt_idempotency_key
    assert client.get_attempt_status(key).status is AttemptStatus.NOT_FOUND

    receipt = client.start_attempt(request)
    assert receipt.status in (
        AttemptStatus.ACCEPTED,
        AttemptStatus.CREATING,
        AttemptStatus.RUNNING,
        AttemptStatus.EXITED,
    )
    assert receipt.deduplicated is False
    assert receipt.attempt_idempotency_key == key
    assert receipt.request_payload_sha256 == request.request_payload_sha256
    assert (
        receipt.authorization_identity_sha256 == request.authorization_identity_sha256
    )
    assert _wait_status(client, key, {AttemptStatus.EXITED}).status is (
        AttemptStatus.EXITED
    )
    (row,) = supervisor.rows()
    assert row[:3] == (key, receipt.supervisor_attempt_id, "EXITED")


# 3-4. Same key: duplicate suppression and conflicts ---------------------------


def test_repeating_the_same_key_returns_the_existing_attempt(supervisor):
    client = supervisor.client()
    request = _start_request()
    first = client.start_attempt(request)
    _wait_status(client, request.attempt_idempotency_key, {AttemptStatus.EXITED})

    for _ in range(3):
        again = client.start_attempt(request)
        assert again.deduplicated is True
        assert again.supervisor_attempt_id == first.supervisor_attempt_id
        assert again.status is AttemptStatus.EXITED
    assert len(supervisor.rows()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"request_payload_sha256": _hex("other-payload")},
        {"authorization_identity_sha256": _hex("other-identity")},
        {"claim_uuid": str(uuid.uuid4())},
        {"task_execution_id": 99},
    ],
)
def test_conflicting_request_under_the_same_key_is_rejected(supervisor, changes):
    client = supervisor.client()
    request = _start_request()
    first = client.start_attempt(request)
    before = supervisor.rows()

    conflict = client.start_attempt(replace(request, **changes))

    assert conflict.status is AttemptStatus.REJECTED
    assert conflict.deduplicated is False
    # The receipt describes the existing attempt, never the conflicting one.
    assert conflict.supervisor_attempt_id == first.supervisor_attempt_id
    assert conflict.request_payload_sha256 == request.request_payload_sha256
    assert supervisor.rows()[0][3] == before[0][3]
    assert len(supervisor.rows()) == 1


# 5-6. Restart recovery and status lookup --------------------------------------


@pytest.mark.parametrize("how", ["terminate", "kill"])
def test_restart_preserves_the_attempt(supervisor, how):
    client = supervisor.client()
    request = _start_request()
    key = request.attempt_idempotency_key
    first = client.start_attempt(request)
    _wait_status(client, key, {AttemptStatus.EXITED})
    old_pid = supervisor.proc.pid

    if how == "terminate":
        assert supervisor.stop() == 0
    else:
        supervisor.crash()
    with pytest.raises(SupervisorTransportError):
        client.get_attempt_status(key)
    supervisor.start()
    assert supervisor.proc.pid != old_pid

    status = client.get_attempt_status(key)
    assert status.status is AttemptStatus.EXITED
    assert status.supervisor_attempt_id == first.supervisor_attempt_id
    again = client.start_attempt(request)
    assert again.deduplicated is True
    assert again.supervisor_attempt_id == first.supervisor_attempt_id
    assert len(supervisor.rows()) == 1


def test_restart_while_running_reconciles_the_same_container(supervisor):
    supervisor.stop()
    supervisor.start(noop_seconds=60)
    client = supervisor.client()
    request = _start_request()
    key = request.attempt_idempotency_key
    start_errors = []

    def start_waiting_attempt():
        try:
            client.start_attempt(request)
        except SupervisorTransportError as exc:
            start_errors.append(exc)

    request_thread = threading.Thread(target=start_waiting_attempt, daemon=True)
    request_thread.start()
    assert _wait_status(client, key, {AttemptStatus.RUNNING}).status is (
        AttemptStatus.RUNNING
    )
    attempt_id = supervisor.rows()[0][1]

    supervisor.crash()
    supervisor.start()
    request_thread.join(timeout=5)
    assert not request_thread.is_alive()
    assert start_errors

    status = client.get_attempt_status(key)
    assert status.status is AttemptStatus.RUNNING
    assert status.supervisor_attempt_id == attempt_id
    time.sleep(0.5)
    assert client.get_attempt_status(key).status is AttemptStatus.RUNNING
    assert supervisor.rows()[0][2] == "RUNNING"


def test_accepted_attempt_that_never_began_runs_once_after_restart(supervisor):
    supervisor.stop()
    # A crash between registration and launch leaves only ACCEPTED.
    request = asdict(_start_request())
    AttemptLedger(supervisor.ledger_path).register(
        request,
        execution_timeout_seconds=120,
        resource_limits={
            "cpu_count": 1.0,
            "memory_bytes": 536870912,
            "pids": 64,
            "timeout_seconds": 120,
            "output_bytes_per_stream": 1048576,
            "workspace_tmpfs_bytes": 67108864,
            "result_tmpfs_bytes": 8388608,
        },
    )
    supervisor.start()
    client = supervisor.client()
    key = request["attempt_idempotency_key"]
    assert _wait_status(client, key, {AttemptStatus.EXITED}).status is (
        AttemptStatus.EXITED
    )
    assert len(supervisor.rows()) == 1


def test_ledger_rows_are_insert_only_and_forward_only(supervisor):
    client = supervisor.client()
    request = _start_request()
    client.start_attempt(request)
    _wait_status(client, request.attempt_idempotency_key, {AttemptStatus.EXITED})
    with sqlite3.connect(supervisor.ledger_path) as raw:
        for statement in (
            "DELETE FROM supervisor_attempts",
            "UPDATE supervisor_attempts SET status = 'ACCEPTED'",
            "UPDATE supervisor_attempts SET request_payload_sha256 = 'x'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO supervisor_attempts SELECT * FROM supervisor_attempts"
            )


# 7. Concurrency ---------------------------------------------------------------


def test_concurrent_duplicate_requests_produce_one_attempt(supervisor):
    client = supervisor.client()
    request = _start_request()
    barrier = threading.Barrier(16)
    receipts, errors = [], []

    def run():
        try:
            barrier.wait(timeout=10)
            receipts.append(client.start_attempt(request))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(receipts) == 16
    assert sum(not receipt.deduplicated for receipt in receipts) == 1
    assert len({receipt.supervisor_attempt_id for receipt in receipts}) == 1
    assert len(supervisor.rows()) == 1


def test_concurrent_clients_in_separate_processes_produce_one_attempt(supervisor):
    request = json.dumps(asdict(_start_request()))
    script = (
        "import json,sys\n"
        "from app.services.execution.one_shot_dispatch import ExecutorStartRequest\n"
        "from app.services.execution.one_shot_supervisor_client import "
        "UnixSocketSupervisor\n"
        "client = UnixSocketSupervisor(sys.argv[1], supervisor_uid=int(sys.argv[2]))\n"
        "r = client.start_attempt(ExecutorStartRequest(**json.loads(sys.argv[3])))\n"
        "print(json.dumps([r.supervisor_attempt_id, r.deduplicated]))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(supervisor.socket_path),
                str(UID),
                request,
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    results = [json.loads(proc.communicate(timeout=120)[0]) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs)
    assert len({attempt_id for attempt_id, _ in results}) == 1
    assert sum(not deduplicated for _, deduplicated in results) == 1
    assert len(supervisor.rows()) == 1


# Authentication and fail-closed behaviour -------------------------------------


def test_unlisted_caller_uid_is_refused(supervisor_root):
    root, processes = supervisor_root
    process = SupervisorProcess(root, allowed_uid=UID + 4242)
    processes.append(process)
    process.start()
    client = process.client()

    admission = client.admission()
    assert admission.status is SupervisorAdmissionStatus.UNTRUSTED
    assert admission.reasons == ("caller_not_authenticated",)
    with pytest.raises(SupervisorTransportError):
        client.start_attempt(_start_request())
    with pytest.raises(SupervisorTransportError):
        client.get_attempt_status(_start_request().attempt_idempotency_key)
    assert process.rows() == []


def test_client_refuses_a_supervisor_with_an_unexpected_uid(supervisor):
    client = supervisor.client(supervisor_uid=UID + 4242)
    admission = client.admission()
    assert admission.status is SupervisorAdmissionStatus.UNTRUSTED
    with pytest.raises(SupervisorTransportError):
        client.start_attempt(_start_request())
    assert supervisor.rows() == []


@pytest.mark.skipif(UID != 0, reason="changing socket ownership needs root")
def test_client_refuses_a_socket_owned_by_another_uid(supervisor):
    # The peer UID still matches; only the socket file's owner differs.
    os.chown(supervisor.socket_path, UID + 4242, -1)
    assert supervisor.client().admission().status is (
        SupervisorAdmissionStatus.UNTRUSTED
    )
    with pytest.raises(SupervisorTransportError):
        supervisor.client().start_attempt(_start_request())
    assert supervisor.rows() == []


def test_client_refuses_a_world_writable_socket(supervisor):
    os.chmod(supervisor.socket_path, 0o666)
    assert supervisor.client().admission().status is (
        SupervisorAdmissionStatus.UNTRUSTED
    )


@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"not json\n", "request_not_json"),
        (b'{"protocol": "other/v1", "op": "admission"}\n', "protocol_unsupported"),
        (
            b'{"protocol": "one-shot-supervisor/v1", "op": "run", "cmd": "id"}\n',
            "operation_unsupported",
        ),
        (b"x" * (70 * 1024) + b"\n", "request_too_large"),
        (b'{"protocol": "one-shot-supervisor/v1", "op": "admission"}', None),
    ],
)
def test_malformed_requests_fail_closed(supervisor, payload, reason):
    response = _raw(supervisor.socket_path, payload)
    assert response["error"] == "invalid_request"
    if reason:
        assert response["reason"] == reason
    assert supervisor.rows() == []
    assert supervisor.proc.poll() is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(command=["/bin/sh", "-c", "id"]),
        lambda r: r.update(image="alpine", mounts=["/:/host"]),
        lambda r: r.pop("claim_uuid"),
        lambda r: r.update(schema_version="executor-start-attempt/v2"),
        lambda r: r.update(execution_profile_id="shell"),
        lambda r: r.update(attempt_idempotency_key="A" * 64),
        lambda r: r.update(claim_uuid="not-a-uuid"),
        lambda r: r.update(task_execution_id=True),
        lambda r: r.update(task_execution_id=0),
    ],
)
def test_invalid_start_requests_are_rejected_without_registration(supervisor, mutate):
    request = asdict(_start_request())
    mutate(request)
    payload = {"protocol": "one-shot-supervisor/v1", "op": "start_attempt"}
    payload["request"] = request
    response = _raw(supervisor.socket_path, json.dumps(payload).encode() + b"\n")
    assert response["error"] == "invalid_request"
    assert supervisor.rows() == []


def test_unavailable_supervisor_raises_transport_errors(supervisor):
    client = supervisor.client()
    supervisor.stop()
    for call in (
        client.admission,
        lambda: client.get_attempt_status(_hex("k")),
        lambda: client.start_attempt(_start_request()),
    ):
        with pytest.raises(SupervisorTransportError):
            call()


def test_only_one_supervisor_per_state_directory(supervisor):
    second = supervisor.spawn()
    assert second.wait(timeout=20) != 0
    assert "another Supervisor" in second.stderr.read()
    assert supervisor.client().admission().status is (SupervisorAdmissionStatus.TRUSTED)


def test_open_state_directory_is_refused(supervisor_root):
    root, processes = supervisor_root
    process = SupervisorProcess(root)
    os.chmod(process.state_dir, 0o755)
    child = process.spawn()
    assert child.wait(timeout=20) != 0
    assert "too open" in child.stderr.read()
    assert not process.socket_path.exists()


# F1c-1 contract integration ----------------------------------------------------


@pytest.fixture
def orchestrator_db(tmp_path: Path, monkeypatch):
    """Disposable migrated SQLite, as in the F1c-1 suite; gate enabled in-test."""

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f2.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    db = sessionmaker(autoflush=False, bind=engine)()
    try:
        yield path, engine, db
    finally:
        db.close()
        engine.dispose()


def _claimed_request(db):
    identity, _ = _issue(db)
    claim = claim_one_shot_authorization(db, identity)
    assert claim.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    return identity, OneShotDispatchRequest.from_claim(identity, claim)


def test_f1c_dispatch_through_the_real_supervisor(orchestrator_db, supervisor):
    path, _, db = orchestrator_db
    _, request = _claimed_request(db)
    client = supervisor.client()
    before = _snapshot_rows(path)

    def dispatch(mode=DispatchMode.START, worker=VERIFIED):
        return dispatch_one_shot_intent(
            db, request, worker_identity=worker, supervisor=client, mode=mode
        )

    # Production posture never reaches the Supervisor.
    refused = dispatch(worker=PRODUCTION_POSTURE)
    assert refused.outcome is DispatchOutcome.CANDIDATE_PIN_NOT_MATCHED
    assert refused.supervisor_contacted is False
    assert supervisor.rows() == []

    first = dispatch()
    assert first.outcome is DispatchOutcome.DISPATCHED
    assert first.start_submitted is True
    key = request.attempt_idempotency_key
    _wait_status(client, key, {AttemptStatus.EXITED})

    repeated = dispatch()
    assert repeated.outcome is DispatchOutcome.RECONCILED
    assert repeated.start_submitted is False
    assert repeated.supervisor_attempt_id == first.supervisor_attempt_id
    reconcile = dispatch(mode=DispatchMode.RECONCILE_ONLY)
    assert reconcile.outcome is DispatchOutcome.RECONCILED
    assert reconcile.attempt_status is AttemptStatus.EXITED

    supervisor.crash()
    held = dispatch(mode=DispatchMode.RECONCILE_ONLY)
    assert held.outcome is DispatchOutcome.SUPERVISOR_UNAVAILABLE
    supervisor.start()
    after_restart = dispatch()
    assert after_restart.outcome is DispatchOutcome.RECONCILED
    assert after_restart.supervisor_attempt_id == first.supervisor_attempt_id

    (row,) = supervisor.rows()
    assert row[1] == first.supervisor_attempt_id
    # The Supervisor never writes Orchestrator rows.
    assert _snapshot_rows(path) == before


def test_f1c_concurrent_dispatchers_produce_one_supervisor_attempt(
    orchestrator_db, supervisor
):
    _, engine, db = orchestrator_db
    _, request = _claimed_request(db)
    client = supervisor.client()
    factory = sessionmaker(autoflush=False, bind=engine)
    barrier = threading.Barrier(4)
    results = []

    def run():
        with factory() as own_db:
            barrier.wait(timeout=10)
            results.append(
                dispatch_one_shot_intent(
                    own_db, request, worker_identity=VERIFIED, supervisor=client
                )
            )

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes.count(DispatchOutcome.DISPATCHED.value) == 1
    assert outcomes.count(DispatchOutcome.RECONCILED.value) == 3
    assert len({result.supervisor_attempt_id for result in results}) == 1
    assert len(supervisor.rows()) == 1


def test_f1c_reconciles_completed_attempt_after_restart(orchestrator_db, supervisor):
    _, _, db = orchestrator_db
    _, request = _claimed_request(db)
    client = supervisor.client()
    first = dispatch_one_shot_intent(
        db, request, worker_identity=VERIFIED, supervisor=client
    )
    assert first.outcome is DispatchOutcome.DISPATCHED
    _wait_status(client, request.attempt_idempotency_key, {AttemptStatus.EXITED})
    supervisor.crash()
    supervisor.start()

    for mode in (DispatchMode.START, DispatchMode.RECONCILE_ONLY):
        held = dispatch_one_shot_intent(
            db, request, worker_identity=VERIFIED, supervisor=client, mode=mode
        )
        assert held.outcome is DispatchOutcome.RECONCILED
        assert held.start_submitted is False
    assert [row[2] for row in supervisor.rows()] == ["EXITED"]


# Isolation ---------------------------------------------------------------------


def test_supervisor_process_is_standard_library_only():
    package = REPO / "app" / "supervisor"
    allowed = set(sys.stdlib_module_names) | {"app"}
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed, (path.name, name)
                if name.startswith("app"):
                    assert name.startswith("app.supervisor"), (path.name, name)
    source = (package / "server.py").read_text(encoding="utf-8")
    assert "subprocess" not in source and "docker" not in source.lower()


def test_worker_is_the_only_importer_of_the_supervisor_adapter():
    root = REPO / "app"
    assert _importers(root, "one_shot_supervisor_client") == {"tasks/worker.py"}
    # The Supervisor process itself is never imported by the Orchestrator.
    outside = {
        path
        for path in _importers(root, "app.supervisor")
        if not path.startswith("supervisor/")
    }
    assert outside == set()
