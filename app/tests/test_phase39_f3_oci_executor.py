"""Focused F3 lifecycle tests; fake-runtime cases are not isolation evidence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import threading
import time
import uuid

import pytest

from app.services.execution.one_shot_dispatch import ExecutorStartRequest
from app.supervisor.ledger import AttemptLedger, deterministic_container_name
from app.supervisor.oci_executor import (
    MAX_CAPTURE_BYTES,
    OCIExecutionError,
    OCIExecutor,
    PodmanRuntime,
    SUPPORTED_SCOPE_SHA256,
)
from app.tests.test_phase39_f1a_authorization import (
    CANDIDATE_FIXTURE_BYTES,
    SOURCE_SNAPSHOT_FIXTURE_BYTES,
)


IMAGE = "localhost/orchestrator-test@sha256:" + hashlib.sha256(b"image").hexdigest()


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(seed: str = "f3") -> dict:
    return asdict(
        ExecutorStartRequest(
            schema_version="executor-start-attempt/v1",
            attempt_idempotency_key=_hex("attempt-" + seed),
            request_payload_sha256=_hex("payload-" + seed),
            authorization_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, "auth-" + seed)),
            authorization_identity_sha256=_hex("identity-" + seed),
            claim_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, "claim-" + seed)),
            project_id=1,
            session_id=2,
            session_instance_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "session-" + seed)),
            task_id=3,
            task_execution_id=4,
            source_revision_identity="git:" + "0" * 40,
            source_snapshot_digest=hashlib.sha256(
                SOURCE_SNAPSHOT_FIXTURE_BYTES
            ).hexdigest(),
            candidate_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, "candidate-" + seed)),
            candidate_initial_digest=hashlib.sha256(
                CANDIDATE_FIXTURE_BYTES
            ).hexdigest(),
            execution_profile_id="executor-contained-planning",
            execution_profile_version="v1",
            operation_scope_id="restricted-python-workspace",
            operation_scope_version="1",
            operation_scope_digest=SUPPORTED_SCOPE_SHA256,
        )
    )


def _put_blob(root: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = root / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o444)
    return digest


class FakeRuntime:
    """Control-plane fake; never executes a candidate on the host."""

    def __init__(self, *, available=True, exit_code=0, delay=0.0, create_error=False):
        self.is_available = available
        self.exit_code = exit_code
        self.delay = delay
        self.create_error = create_error
        self.containers = {}
        self.create_count = 0
        self.remove_succeeds = True
        self.last_create = None

    def available(self):
        return self.is_available

    def image_exists(self, image):
        return self.is_available

    def create(self, *, name, image, input_dir, source_dir, labels, limits):
        self.create_count += 1
        self.last_create = {
            "name": name,
            "image": image,
            "input_dir": input_dir,
            "source_dir": source_dir,
            "labels": labels,
            "limits": limits,
        }
        if self.create_error:
            raise OCIExecutionError("runtime_command_failed")
        self.containers[name] = {
            "Id": "container-" + str(self.create_count),
            "Name": name,
            "Config": {"Labels": dict(labels)},
            "State": {"Status": "created", "ExitCode": None},
            "started": None,
        }
        return self.containers[name]["Id"]

    def start(self, name):
        container = self.containers[name]
        container["started"] = time.monotonic()
        container["State"].update(
            Status="running",
            StartedAt=datetime.now(timezone.utc).isoformat(),
            ExitCode=None,
        )
        if self.delay <= 0:
            container["State"].update(Status="exited", ExitCode=self.exit_code)

    def inspect(self, name):
        container = self.containers.get(name)
        if container is None:
            return None
        if (
            container["State"]["Status"] == "running"
            and time.monotonic() - container["started"] >= self.delay
        ):
            container["State"].update(Status="exited", ExitCode=self.exit_code)
        return container

    def stop(self, name, *, grace_seconds=2):
        container = self.containers.get(name)
        if container is not None:
            container["State"].update(Status="exited", ExitCode=143)

    def remove(self, name):
        if not self.remove_succeeds:
            return False
        self.containers.pop(name, None)
        return True

    def copy_from(self, name, source, destination):
        if name not in self.containers:
            return False
        Path(destination).write_bytes(
            ("captured " + source.rsplit("/", 1)[-1]).encode()
        )
        return True

    def list_owned(self):
        return list(self.containers.values())


def _executor(tmp_path, runtime=None, *, timeout=10):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    store = tmp_path / "content"
    store.mkdir(mode=0o700, exist_ok=True)
    source_digest = _put_blob(store, SOURCE_SNAPSHOT_FIXTURE_BYTES)
    candidate_digest = _put_blob(store, CANDIDATE_FIXTURE_BYTES)
    ledger = AttemptLedger(state_dir / "attempts.sqlite3")
    ledger.initialize()
    executor = OCIExecutor(
        ledger=ledger,
        state_dir=state_dir,
        content_store_dir=store,
        image=IMAGE,
        runtime="/not-installed/podman",
        timeout_seconds=timeout,
        poll_interval_seconds=0.05,
    )
    if runtime is not None:
        executor.runtime = runtime
    request = _request()
    request["source_snapshot_digest"] = source_digest
    request["candidate_initial_digest"] = candidate_digest
    registration = ledger.register(
        request,
        execution_timeout_seconds=timeout,
        resource_limits=executor.resource_limits,
    )
    return executor, ledger, request, registration.record


def _wait_for(ledger, key, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = ledger.get(key)
        if record is not None and record.status == status:
            return record
        time.sleep(0.02)
    return ledger.get(key)


def test_success_persists_bounded_structured_result_and_cleanup(tmp_path):
    runtime = FakeRuntime()
    executor, ledger, request, record = _executor(tmp_path, runtime)

    executor.execute_or_reconcile(record.attempt_idempotency_key)

    terminal = AttemptLedger(ledger.path).get(record.attempt_idempotency_key)
    assert terminal.status == "EXITED"
    assert terminal.exit_code == 0
    assert terminal.started_at and terminal.completed_at
    assert terminal.cleanup_status == "SUCCEEDED"
    assert terminal.stdout_bytes == len(b"captured stdout")
    assert terminal.stderr_bytes == len(b"captured stderr")
    result = executor.result_document(terminal)
    assert result["execution_id"] == record.supervisor_attempt_id
    assert result["authorization_uuid"] == request["authorization_uuid"]
    assert result["claim_uuid"] == request["claim_uuid"]
    assert result["terminal_status"] == "EXITED"
    assert result["timed_out"] is False
    assert result["stdout"]["limit_bytes"] == MAX_CAPTURE_BYTES
    assert result["cleanup"]["status"] == "SUCCEEDED"
    assert runtime.create_count == 1


def test_nonzero_exit_is_durable_failure(tmp_path):
    runtime = FakeRuntime(exit_code=7)
    executor, ledger, _, record = _executor(tmp_path, runtime)

    executor.execute_or_reconcile(record.attempt_idempotency_key)

    result = ledger.get(record.attempt_idempotency_key)
    assert result.status == "FAILED"
    assert result.exit_code == 7
    assert result.status_reason == "operation_nonzero_exit"


def test_runtime_unavailable_refuses_admission_before_creation(tmp_path):
    runtime = FakeRuntime(available=False)
    executor, _, _, record = _executor(tmp_path, runtime)

    available, reasons = executor.admission()

    assert available is False
    assert reasons == ("oci_runtime_unavailable",)
    assert runtime.create_count == 0
    assert record.status == "ACCEPTED"


def test_container_creation_failure_is_terminal_without_host_fallback(tmp_path):
    runtime = FakeRuntime(create_error=True)
    executor, ledger, _, record = _executor(tmp_path, runtime)

    executor.execute_or_reconcile(record.attempt_idempotency_key)

    failed = ledger.get(record.attempt_idempotency_key)
    assert failed.status == "FAILED"
    assert failed.status_reason == "runtime_command_failed"
    assert failed.cleanup_status == "NOT_REQUIRED"
    assert runtime.create_count == 1


def test_timeout_stops_container_and_persists_timeout_classification(tmp_path):
    runtime = FakeRuntime(delay=30.0)
    executor, ledger, _, record = _executor(tmp_path, runtime, timeout=1)

    thread = threading.Thread(
        target=executor.execute_or_reconcile,
        args=(record.attempt_idempotency_key,),
        daemon=True,
    )
    thread.start()
    assert _wait_for(ledger, record.attempt_idempotency_key, "RUNNING") is not None
    thread.join(timeout=4)

    timed_out = ledger.get(record.attempt_idempotency_key)
    assert not thread.is_alive()
    assert timed_out.status == "TIMED_OUT"
    assert timed_out.status_reason == "timeout"
    assert timed_out.cleanup_status == "SUCCEEDED"
    assert executor.result_document(timed_out)["timed_out"] is True


def test_cancellation_is_durable_and_does_not_relaunch(tmp_path):
    runtime = FakeRuntime(delay=30.0)
    executor, ledger, _, record = _executor(tmp_path, runtime)
    thread = threading.Thread(
        target=executor.execute_or_reconcile,
        args=(record.attempt_idempotency_key,),
        daemon=True,
    )
    thread.start()
    assert _wait_for(ledger, record.attempt_idempotency_key, "RUNNING") is not None

    executor.cancel(record.attempt_idempotency_key)
    thread.join(timeout=3)

    cancelled = ledger.get(record.attempt_idempotency_key)
    assert not thread.is_alive()
    assert cancelled.status == "CANCELLED"
    assert cancelled.cancel_requested_at is not None
    assert cancelled.cleanup_status == "SUCCEEDED"
    executor.execute_or_reconcile(record.attempt_idempotency_key)
    assert runtime.create_count == 1


def test_resource_and_isolation_flags_are_explicit_and_credentials_are_not_inherited(
    tmp_path, monkeypatch
):
    runtime = PodmanRuntime("podman", state_dir=tmp_path)
    captured = []
    monkeypatch.setattr(runtime, "_run", lambda args, **kwargs: captured.append(args))
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda name: {"Id": "id", "State": {"Status": "created"}},
    )
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    env = runtime._environment()

    runtime.create(
        name="orc-one-shot-test",
        image=IMAGE,
        input_dir=tmp_path / "input",
        source_dir=tmp_path / "source",
        labels={"io.orchestrator.supervisor": "phase39-f3/v1"},
        limits=OCIExecutor(
            ledger=AttemptLedger(tmp_path / "unused.sqlite3"),
            state_dir=tmp_path,
            content_store_dir=tmp_path,
            image=IMAGE,
        ).limits,
    )

    args = captured[0]
    joined = " ".join(args)
    assert "--network=none" in args
    assert "--pid=private" in args
    assert "--read-only" in args
    assert "--read-only-tmpfs=false" in args
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--cpus=1" in args
    assert "--memory=536870912" in args
    assert "--memory-swap=536870912" in args
    assert "--pids-limit=64" in args
    assert "--log-driver=none" in args
    assert "--privileged" not in args
    assert "/var/run/docker.sock" not in joined
    assert "/:/host" not in joined
    assert "OPENAI_API_KEY" not in env
    assert "host-secret" not in joined


def test_recovery_adopts_created_container_and_never_creates_a_second(tmp_path):
    runtime = FakeRuntime()
    executor, ledger, _, record = _executor(tmp_path, runtime)
    assert ledger.transition(record.attempt_idempotency_key, "ACCEPTED", "CREATING")
    creating = ledger.get(record.attempt_idempotency_key)
    input_dir, source_dir = executor._prepare_inputs(creating)
    runtime.create(
        name=creating.container_name,
        image=executor.image,
        input_dir=input_dir,
        source_dir=source_dir,
        labels=executor._labels(creating),
        limits=executor.limits,
    )

    executor.execute_or_reconcile(record.attempt_idempotency_key)

    terminal = ledger.get(record.attempt_idempotency_key)
    assert terminal.status == "EXITED"
    assert runtime.create_count == 1
    assert terminal.container_id == "container-1"


def test_orphan_reconciliation_removes_only_verifiably_owned_containers(tmp_path):
    runtime = FakeRuntime()
    executor, _, _, _ = _executor(tmp_path, runtime)
    orphan_key = _hex("orphan")
    name = deterministic_container_name(orphan_key)
    runtime.containers[name] = {
        "Id": "orphan-id",
        "Name": name,
        "Config": {
            "Labels": {
                "io.orchestrator.supervisor": "phase39-f3/v1",
                "io.orchestrator.attempt-key": orphan_key,
            }
        },
        "State": {"Status": "running", "ExitCode": None},
    }
    runtime.containers["unverified"] = {
        "Id": "other-id",
        "Name": "unverified",
        "Config": {"Labels": {}},
        "State": {"Status": "running", "ExitCode": None},
    }

    executor.reconcile_startup()

    assert name not in runtime.containers
    assert "unverified" in runtime.containers


def test_cleanup_failure_is_persisted_after_terminal_state(tmp_path):
    runtime = FakeRuntime()
    runtime.remove_succeeds = False
    executor, ledger, _, record = _executor(tmp_path, runtime)

    executor.execute_or_reconcile(record.attempt_idempotency_key)

    terminal = ledger.get(record.attempt_idempotency_key)
    assert terminal.status == "EXITED"
    assert terminal.cleanup_status == "FAILED"
    assert terminal.cleanup_reason == "runtime_container_remove_failed"


def test_terminal_row_without_persisted_container_id_reconciles_owned_container(
    tmp_path,
):
    runtime = FakeRuntime()
    executor, ledger, _, record = _executor(tmp_path, runtime)
    runtime.containers[record.container_name] = {
        "Id": "created-before-ledger-write",
        "Name": record.container_name,
        "Config": {"Labels": executor._labels(record)},
        "State": {"Status": "created", "ExitCode": None},
        "started": None,
    }
    assert ledger.transition(record.attempt_idempotency_key, "ACCEPTED", "FAILED")
    failed = ledger.get(record.attempt_idempotency_key)
    assert failed.container_id is None

    executor._cleanup(failed)

    assert record.container_name not in runtime.containers
    assert ledger.get(record.attempt_idempotency_key).cleanup_status == "SUCCEEDED"


def test_source_snapshot_archive_rejects_parent_traversal(tmp_path):
    runtime = FakeRuntime()
    executor, ledger, _, record = _executor(tmp_path, runtime)
    malicious = tmp_path / "malicious.tar"
    with tarfile.open(malicious, "w") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 4
        import io

        archive.addfile(info, io.BytesIO(b"nope"))

    with pytest.raises(OCIExecutionError, match="snapshot_path_invalid"):
        executor._extract_snapshot(malicious.read_bytes(), tmp_path / "unsafe")
    assert not (tmp_path / "outside").exists()
    assert ledger.get(record.attempt_idempotency_key).status == "ACCEPTED"


def test_f2_ledger_upgrade_marks_ambiguous_inflight_rows_unknown(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    request = _request("legacy")
    now = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(path) as raw:
        raw.executescript(
            "CREATE TABLE supervisor_attempts ("
            "attempt_idempotency_key TEXT PRIMARY KEY, supervisor_attempt_id TEXT UNIQUE, "
            "start_request_sha256 TEXT, request_payload_sha256 TEXT, "
            "authorization_uuid TEXT, authorization_identity_sha256 TEXT, "
            "claim_uuid TEXT, operation_id TEXT, start_request_json TEXT, status TEXT, "
            "exit_code INTEGER, status_reason TEXT, created_at TEXT, updated_at TEXT);"
            "CREATE TRIGGER supervisor_attempts_no_delete BEFORE DELETE ON supervisor_attempts "
            "BEGIN SELECT RAISE(ABORT, 'insert only'); END;"
            "CREATE TRIGGER supervisor_attempts_forward_only BEFORE UPDATE ON supervisor_attempts "
            "BEGIN SELECT RAISE(ABORT, 'forward only'); END;"
            "PRAGMA user_version = 1;"
        )
        for index, status in enumerate(("ACCEPTED", "RUNNING", "EXITED")):
            key = _hex(f"legacy-{index}")
            raw.execute(
                "INSERT INTO supervisor_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    str(uuid.uuid4()),
                    _hex(f"request-{index}"),
                    request["request_payload_sha256"],
                    request["authorization_uuid"],
                    request["authorization_identity_sha256"],
                    request["claim_uuid"],
                    "phase39-f2-fixed-noop/v1",
                    json.dumps(request),
                    status,
                    0 if status == "EXITED" else None,
                    None,
                    now,
                    now,
                ),
            )

    ledger = AttemptLedger(path)
    ledger.initialize()
    rows = ledger.all_records()
    assert [row.status for row in rows].count("UNKNOWN") == 2
    assert [row.status for row in rows].count("EXITED") == 1
    legacy_inflight = [row for row in rows if row.status == "UNKNOWN"]
    assert all(
        row.status_reason == "legacy_f2_attempt_not_replayed" for row in legacy_inflight
    )
    legacy_exited = next(row for row in rows if row.status == "EXITED")
    assert legacy_exited.cleanup_status == "NOT_REQUIRED"


def test_scope_digest_mismatch_is_rejected_before_runtime_creation(tmp_path):
    runtime = FakeRuntime()
    executor, _, request, record = _executor(tmp_path, runtime)
    request["operation_scope_digest"] = _hex("different-policy")

    with pytest.raises(OCIExecutionError, match="operation_scope_digest_unsupported"):
        executor.validate_request(request)
    assert runtime.create_count == 0
    assert record.status == "ACCEPTED"


@pytest.mark.integration
def test_real_podman_isolation_when_runtime_and_pinned_image_are_available(
    tmp_path, monkeypatch
):
    runtime_path = shutil.which("podman")
    image = os.environ.get("PHASE39_OCI_TEST_IMAGE", "")
    if not runtime_path:
        pytest.skip("Podman is not installed; live OCI isolation is unavailable")
    if "@sha256:" not in image:
        pytest.skip(
            "PHASE39_OCI_TEST_IMAGE must name a preloaded digest-pinned Python image"
        )

    host_secret = tmp_path / "host-secret"
    host_secret.write_text("must-not-be-mounted", encoding="utf-8")
    candidate = (
        "import os, pathlib\n"
        f"assert pathlib.Path({str(host_secret)!r}).exists() is False\n"
        "assert pathlib.Path(os.environ['ORCHESTRATOR_SOURCE'], 'README.md').read_text()\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "print('isolated candidate complete')\n"
    ).encode()
    snapshot_buffer = io.BytesIO()
    with tarfile.open(fileobj=snapshot_buffer, mode="w") as archive:
        data = b"approved source snapshot\n"
        item = tarfile.TarInfo("README.md")
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    store = tmp_path / "content"
    store.mkdir(mode=0o700)
    source_digest = _put_blob(store, snapshot_buffer.getvalue())
    candidate_digest = _put_blob(store, candidate)
    ledger = AttemptLedger(state / "attempts.sqlite3")
    ledger.initialize()
    executor = OCIExecutor(
        ledger=ledger,
        state_dir=state,
        content_store_dir=store,
        image=image,
        runtime=runtime_path,
        timeout_seconds=15,
    )
    request = _request("live")
    request["source_snapshot_digest"] = source_digest
    request["candidate_initial_digest"] = candidate_digest
    registration = ledger.register(
        request,
        execution_timeout_seconds=15,
        resource_limits=executor.resource_limits,
    )
    available, reasons = executor.admission()
    if not available:
        pytest.skip(
            "OCI runtime or requested image is unavailable: " + ",".join(reasons)
        )
    executor.execute_or_reconcile(registration.record.attempt_idempotency_key)
    terminal = ledger.get(registration.record.attempt_idempotency_key)
    assert terminal.status == "EXITED"
    assert terminal.cleanup_status == "SUCCEEDED"
    assert host_secret.read_text(encoding="utf-8") == "must-not-be-mounted"
