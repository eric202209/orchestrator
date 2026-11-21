from __future__ import annotations

import time
import threading
import uuid
import secrets
import hashlib
import os
import stat
import textwrap

import pytest

from app.services.sandbox_bridge.contract import (
    MAX_OUTPUT_BYTES,
    ExecutionRequest,
    ExecutionResult,
    PROFILE,
    RequestRejected,
)
from app.services.sandbox_bridge.transport import (
    HostSocketLauncher,
    SandboxClient,
    _receive,
    _send,
)
from app.services.sandbox_bridge.docker_executor import DockerExecutor, IMAGE
from app.services.sandbox_bridge.staging import (
    StagedWorkspace,
    StagingRejected,
    validate_artifact_path,
)
from app.services.sandbox_bridge.ledger import RequestLedger
from app.services.sandbox_bridge.ipc_contract import classify_ipc_observation


def ipc_inspection(**changes):
    value = {
        "Id": "container-id",
        "Name": "/probe-container",
        "HostConfig": {"IpcMode": "none"},
        "Mounts": [{"Destination": "/ws", "RW": True}],
    }
    value.update(changes)
    return value


def test_ipc_absent_with_verified_isolation_passes():
    assert (
        classify_ipc_observation(
            {"path_state": "absent", "mounted": False},
            ipc_inspection(),
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "PASS"
    )


def test_ipc_absent_without_verified_isolation_is_invalid():
    assert (
        classify_ipc_observation(
            {"path_state": "absent", "mounted": False},
            None,
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "INVALID_PRECONDITION"
    )


def test_ipc_present_nonwritable_with_verified_isolation_passes():
    assert (
        classify_ipc_observation(
            {"path_state": "present", "mounted": False, "writable": False},
            ipc_inspection(),
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "PASS"
    )


def test_ipc_present_writable_fails():
    assert (
        classify_ipc_observation(
            {"path_state": "present", "mounted": False, "writable": True},
            ipc_inspection(),
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "FAIL"
    )


def test_ipc_unexpected_mount_fails():
    assert (
        classify_ipc_observation(
            {"path_state": "present", "mounted": True, "writable": False},
            ipc_inspection(),
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "FAIL"
    )


def test_ipc_inspection_identity_mismatch_is_invalid():
    assert (
        classify_ipc_observation(
            {"path_state": "absent", "mounted": False},
            ipc_inspection(),
            expected_id="other-id",
            expected_name="probe-container",
        )
        == "INVALID_PRECONDITION"
    )


def test_ipc_missing_or_invalid_inspection_evidence_is_invalid():
    assert (
        classify_ipc_observation(
            {"path_state": "absent", "mounted": False},
            {"Id": "container-id"},
            expected_id="container-id",
            expected_name="probe-container",
        )
        == "INVALID_PRECONDITION"
    )


def request(**changes):
    values = dict(
        request_id=str(uuid.uuid4()),
        task_id="task-1",
        execution_id="exec-1",
        workspace_ref=str(uuid.uuid4()),
        execution_profile=PROFILE,
        code="print('trusted synthetic probe')",
        timeout_seconds=5,
        issued_at=int(time.time()),
        cancellation_token=secrets.token_hex(32),
        version=1,
    )
    values.update(changes)
    return ExecutionRequest(**values)


def test_typed_synthetic_request_round_trips():
    req = request()
    assert ExecutionRequest.from_wire(req.to_wire()).request_id == req.request_id


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"execution_profile": "host"}, "unauthorized_execution_profile"),
        ({"workspace_ref": "../../etc"}, "invalid_workspace_ref"),
        ({"timeout_seconds": 121}, "invalid_timeout"),
        ({"issued_at": 1}, "stale_request"),
        ({"code": "x" * (64 * 1024 + 1)}, "invalid_code_size"),
    ],
)
def test_security_sensitive_fields_fail_closed(changes, code):
    with pytest.raises(RequestRejected, match=code):
        request(**changes).validate()


def test_wire_contract_rejects_docker_arguments_and_extra_fields():
    wire = request().to_wire()
    wire["docker_args"] = ["--privileged", "-v", "/:/host"]
    with pytest.raises(RequestRejected, match="invalid_request_shape"):
        ExecutionRequest.from_wire(wire)


def test_missing_launcher_returns_unavailable_without_local_fallback(tmp_path):
    req = request()
    result = SandboxClient(tmp_path / "missing.sock", b"k" * 32).execute(req)
    assert (result.execution_status, result.error_code, result.request_id) == (
        "UNAVAILABLE",
        "launcher_unavailable",
        req.request_id,
    )


def test_client_retries_only_pre_submission_listener_startup_window(tmp_path):
    import socket

    path = tmp_path / "starting.sock"
    bound = threading.Event()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def delayed_listener():
        listener.bind(str(path))
        bound.set()
        time.sleep(0.05)
        listener.listen(1)
        conn, _ = listener.accept()
        with conn:
            _receive(conn)
            _send(
                conn,
                {
                    "request_id": req.request_id,
                    "execution_status": "SUCCESS",
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "truncated": False,
                    "error_code": None,
                    "duration": 0.0,
                    "artifact_manifest": [],
                    "artifacts": [],
                },
            )
        listener.close()

    req = request()
    thread = threading.Thread(target=delayed_listener, daemon=True)
    thread.start()
    assert bound.wait(1)
    result = SandboxClient(path, b"s" * 32, connect_timeout=0.5).execute(req)
    thread.join(2)
    assert result.execution_status == "SUCCESS" and result.stdout == "ok"


def test_unix_transport_executes_only_through_injected_sandbox_and_rejects_replay(
    tmp_path,
):
    socket_path = tmp_path / "launcher.sock"
    key = b"k" * 32
    calls = []

    def synthetic_executor(req, cancel):
        calls.append(req.request_id)
        return ExecutionResult(
            req.request_id, "SUCCESS", 0, "synthetic-ok", "E" * (MAX_OUTPUT_BYTES + 10)
        )

    launcher = HostSocketLauncher(
        socket_path, key, __import__("os").getuid(), synthetic_executor
    )
    stop = threading.Event()
    server = threading.Thread(target=launcher.serve_forever, args=(stop,), daemon=True)
    server.start()
    deadline = time.monotonic() + 2
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    client = SandboxClient(socket_path, key)
    req = request()
    first = client.execute(req)
    replay = client.execute(req)
    stop.set()
    server.join(timeout=2)
    assert (first.execution_status, first.stdout, first.truncated, calls) == (
        "SUCCESS",
        "synthetic-ok",
        True,
        [req.request_id],
    )
    assert len(first.stderr.encode()) == MAX_OUTPUT_BYTES
    assert (replay.execution_status, replay.error_code) == (
        "REJECTED",
        "duplicate_request",
    )
    assert not socket_path.exists()


def test_fixed_docker_profile_has_no_request_controlled_arguments(tmp_path):
    executor = DockerExecutor(tmp_path)
    argv = executor._argv("p43-sbx-" + uuid.uuid4().hex, tmp_path)
    joined = " ".join(argv)
    for required in (
        "--pull=never",
        "linux/arm64",
        "--network=none",
        "--ipc=none",
        "--read-only",
        "--cap-drop=ALL",
        "no-new-privileges",
        "65534:65534",
        "--cpus=1",
        "512m",
        "--memory-swap=512m",
        "--pids-limit=64",
        "nofile=256:256",
        "fsize=16777216:16777216",
        IMAGE,
    ):
        assert required in joined
    assert "--pid=private" not in joined and "--privileged" not in joined
    assert argv[-3:] == ["python3", "-I", "/ws/input.py"]


def test_host_paths_and_artifact_traversal_are_rejected():
    with pytest.raises(RequestRejected):
        request(workspace_ref="/etc/passwd").validate()
    for path in ("../secret", "/tmp/x", "a/../../b", "a\\b"):
        with pytest.raises(StagingRejected):
            validate_artifact_path(path)


def test_staging_is_unique_private_and_cleans_up(tmp_path):
    previous_umask = os.umask(0o077)
    try:
        a = StagedWorkspace(tmp_path / "stage", "print(1 + 1)")
        b = StagedWorkspace(tmp_path / "stage", "print(1 + 1)")
    finally:
        os.umask(previous_umask)
    assert a.path != b.path
    assert stat.S_IMODE(a.path.stat().st_mode) == 0o755
    assert stat.S_IMODE(a.input_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(a.output_path.stat().st_mode) == 0o1777
    assert a.input_path.read_text() == "print(1 + 1)"
    a.cleanup()
    b.cleanup()
    assert not a.path.exists() and not b.path.exists()


def test_staging_rejects_symlink_hardlink_special_and_excessive_artifacts(tmp_path):
    ws = StagedWorkspace(tmp_path / "stage", "print(1)")
    (ws.output_path / "escape").symlink_to(ws.input_path)
    with pytest.raises(StagingRejected, match="unsafe_artifact_entry"):
        ws.collect()
    (ws.output_path / "escape").unlink()
    (ws.output_path / "one").write_bytes(b"x")
    os.link(ws.output_path / "one", ws.output_path / "two")
    with pytest.raises(StagingRejected, match="unsafe_artifact_entry"):
        ws.collect()
    (ws.output_path / "two").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(ws.output_path / "pipe")
        with pytest.raises(StagingRejected, match="unsafe_artifact_entry"):
            ws.collect()
    ws.cleanup()


def test_artifact_manifest_hash_verified_over_transport(tmp_path):
    path, key = tmp_path / "bridge.sock", b"a" * 32

    def executor(req, _cancel):
        data = b"synthetic artifact"
        return ExecutionResult(
            req.request_id,
            "SUCCESS",
            0,
            "2",
            "",
            artifact_manifest=[
                {
                    "path": "answer.txt",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
            artifacts=[data],
        )

    launcher = HostSocketLauncher(path, key, os.getuid(), executor)
    stop = threading.Event()
    thread = threading.Thread(target=launcher.serve_forever, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    result = SandboxClient(path, key).execute(request())
    stop.set()
    thread.join(timeout=2)
    assert result.execution_status == "SUCCESS" and result.artifacts == [
        b"synthetic artifact"
    ]


def test_artifact_hash_mismatch_is_rejected(tmp_path):
    path, key = tmp_path / "bridge.sock", b"b" * 32

    def executor(req, _cancel):
        return ExecutionResult(
            req.request_id,
            "SUCCESS",
            0,
            "",
            "",
            artifact_manifest=[{"path": "x", "size": 1, "sha256": "0" * 64}],
            artifacts=[b"x"],
        )

    launcher = HostSocketLauncher(path, key, os.getuid(), executor)
    stop = threading.Event()
    thread = threading.Thread(target=launcher.serve_forever, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(RequestRejected, match="artifact_hash_mismatch"):
        SandboxClient(path, key).execute(request())
    stop.set()
    thread.join(timeout=2)


def test_durable_ledger_rejects_duplicate_after_restart_and_keeps_uncertain(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    req_id = str(uuid.uuid4())
    first = RequestLedger(path)
    assert first.admit(req_id)
    second = RequestLedger(path)
    assert not second.admit(req_id)
    assert second.status(req_id) == "INTERNAL_ERROR"
    second.terminal(req_id, "SUCCESS")
    assert second.status(req_id) == "SUCCESS"


def test_ledger_bounded_terminal_retention_keeps_replay_tombstones(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.sqlite3", retention=1)
    old, latest = str(uuid.uuid4()), str(uuid.uuid4())
    assert ledger.admit(old)
    ledger.terminal(old, "SUCCESS")
    assert ledger.admit(latest)
    ledger.terminal(latest, "SUCCESS")
    assert ledger.status(old) is None
    assert ledger.admit(old) is False


def test_launcher_restart_rejects_previously_accepted_request(tmp_path):
    sock, ledger_path, key = (
        tmp_path / "bridge.sock",
        tmp_path / "ledger.sqlite3",
        b"r" * 32,
    )
    calls = []

    def execute(req, _cancel):
        calls.append(req.request_id)
        return ExecutionResult(req.request_id, "SUCCESS", 0, "ok", "")

    req = request(issued_at=int(time.time()) + 5)
    client = SandboxClient(sock, key)
    first_launcher = HostSocketLauncher(sock, key, os.getuid(), execute, ledger_path)
    stop1 = threading.Event()
    thread1 = threading.Thread(
        target=first_launcher.serve_forever, args=(stop1,), daemon=True
    )
    thread1.start()
    deadline = time.monotonic() + 2
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    first = client.execute(req)
    stop1.set()
    thread1.join(2)
    second_launcher = HostSocketLauncher(sock, key, os.getuid(), execute, ledger_path)
    stop2 = threading.Event()
    thread2 = threading.Thread(
        target=second_launcher.serve_forever, args=(stop2,), daemon=True
    )
    thread2.start()
    deadline = time.monotonic() + 2
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    replay = client.execute(req)
    stop2.set()
    thread2.join(2)
    assert first.execution_status == "SUCCESS"
    assert (
        replay.execution_status == "REJECTED"
        and replay.error_code == "duplicate_request"
    )
    assert calls == [req.request_id]


@pytest.mark.parametrize("count,size", [(9, 0), (1, 1024 * 1024 + 1)])
def test_artifact_count_and_individual_size_limits(tmp_path, count, size):
    ws = StagedWorkspace(tmp_path / "stage", "print(1)")
    if size:
        (ws.output_path / "big").write_bytes(b"x" * size)
    else:
        for i in range(count):
            (ws.output_path / f"{i}.txt").write_bytes(b"x")
    with pytest.raises(StagingRejected, match="artifact_limit_exceeded"):
        ws.collect()
    ws.cleanup()


def test_authorized_cancel_works_and_wrong_secret_does_not(tmp_path):
    path, key = tmp_path / "bridge.sock", b"c" * 32
    entered, release = threading.Event(), threading.Event()

    def executor(req, cancel):
        entered.set()
        while not cancel.is_set() and not release.wait(0.01):
            pass
        return ExecutionResult(
            req.request_id, "CANCELLED" if cancel.is_set() else "SUCCESS", None, "", ""
        )

    launcher = HostSocketLauncher(path, key, os.getuid(), executor)
    stop = threading.Event()
    server = threading.Thread(target=launcher.serve_forever, args=(stop,), daemon=True)
    server.start()
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    req = request()
    client = SandboxClient(path, key)
    result_box = []
    worker = threading.Thread(
        target=lambda: result_box.append(client.execute(req)), daemon=True
    )
    worker.start()
    assert entered.wait(2)
    assert client.cancel(req.request_id, secrets.token_hex(32)) is False
    assert client.cancel(req.request_id, req.cancellation_token) is True
    worker.join(2)
    stop.set()
    server.join(2)
    assert result_box[0].execution_status == "CANCELLED"


def test_docker_unavailable_fails_closed_without_local_fallback(tmp_path, monkeypatch):
    import app.services.sandbox_bridge.docker_executor as module

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("docker missing")

    monkeypatch.setattr(module.subprocess, "Popen", unavailable)
    called = request()
    result = DockerExecutor(tmp_path / "stage", docker="missing-docker").execute(
        called, threading.Event()
    )
    assert result.execution_status == "UNAVAILABLE"
    assert not list((tmp_path / "stage").glob("request-*/input.py"))


def _fake_docker(path, mode="success"):
    script = path / "fake-docker"
    script.write_text(
        textwrap.dedent(
            f"""\
        #!/usr/bin/env python3
        import os, signal, sys, time
        from pathlib import Path
        args = sys.argv[1:]
        marker = Path({str(path / 'container.pid')!r})
        events = Path({str(path / 'events')!r})
        op = args[0]
        with events.open('a') as log: log.write(op + "\\n")
        if op == "run":
            marker.write_text(str(os.getpid()))
            if {mode!r} == "timeout":
                signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
                while True: time.sleep(.05)
            if {mode!r} == "failure":
                print("synthetic failure", file=sys.stderr); sys.exit(17)
            mount = next(x for x in args if x.startswith("type=bind,src="))
            root = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            (root / "output" / "answer.txt").write_bytes(b"42\\n")
            print("2")
            sys.exit(0)
        if op in ("stop", "kill"):
            if marker.exists():
                try: os.kill(int(marker.read_text()), signal.SIGTERM if op == "stop" else signal.SIGKILL)
                except ProcessLookupError: pass
            sys.exit(0)
        if op == "rm":
            marker.unlink(missing_ok=True); sys.exit(0)
        if op == "inspect": sys.exit(1)
        sys.exit(0)
    """
        )
    )
    script.chmod(0o755)
    return script


def test_fake_docker_success_returns_artifact_and_cleans_container_workspace(tmp_path):
    fake = _fake_docker(tmp_path)
    root = tmp_path / "staging"
    result = DockerExecutor(root, docker=str(fake)).execute(
        request(), threading.Event()
    )
    assert result.execution_status == "SUCCESS" and result.stdout.strip() == "2"
    assert result.artifacts == [b"42\n"]
    assert (
        "run" in (tmp_path / "events").read_text()
        and "rm" in (tmp_path / "events").read_text()
    )
    assert not (tmp_path / "container.pid").exists()
    assert list(root.iterdir()) == []


def test_fake_docker_timeout_stops_container_and_cleans_workspace(tmp_path):
    fake = _fake_docker(tmp_path, "timeout")
    root = tmp_path / "staging"
    req = request(timeout_seconds=1)
    result = DockerExecutor(root, docker=str(fake)).execute(req, threading.Event())
    events = (tmp_path / "events").read_text()
    assert result.execution_status == "TIMEOUT"
    assert "stop" in events and "rm" in events
    assert not (tmp_path / "container.pid").exists() and list(root.iterdir()) == []


def test_fake_docker_authorized_cancellation_stops_container(tmp_path):
    fake = _fake_docker(tmp_path, "timeout")
    cancellation = threading.Event()
    timer = threading.Timer(0.3, cancellation.set)
    timer.start()
    result = DockerExecutor(tmp_path / "staging", docker=str(fake)).execute(
        request(timeout_seconds=5), cancellation
    )
    timer.join()
    events = (tmp_path / "events").read_text()
    assert result.execution_status == "CANCELLED"
    assert "stop" in events and "rm" in events
    assert not (tmp_path / "container.pid").exists()


def test_fake_docker_failure_is_execution_failure_with_cleanup(tmp_path):
    fake = _fake_docker(tmp_path, "failure")
    root = tmp_path / "staging"
    result = DockerExecutor(root, docker=str(fake)).execute(
        request(), threading.Event()
    )
    assert result.execution_status == "EXECUTION_FAILED" and result.exit_code == 17
    assert "rm" in (tmp_path / "events").read_text()
    assert not (tmp_path / "container.pid").exists() and list(root.iterdir()) == []


def test_client_rejects_uncorrelated_result(tmp_path):
    sock_path = tmp_path / "manual.sock"
    listener = __import__("socket").socket(
        __import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM
    )
    listener.bind(str(sock_path))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        with conn:
            # Drain request frame, then return a structurally valid wrong id.
            from app.services.sandbox_bridge.transport import _receive

            _receive(conn)
            _send(
                conn,
                {
                    "request_id": str(uuid.uuid4()),
                    "execution_status": "SUCCESS",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "truncated": False,
                    "error_code": None,
                    "duration": 0.0,
                    "artifact_manifest": [],
                    "artifacts": [],
                },
            )
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with pytest.raises(RequestRejected, match="result_request_mismatch"):
        SandboxClient(sock_path, b"m" * 32).execute(request())
    thread.join(2)
