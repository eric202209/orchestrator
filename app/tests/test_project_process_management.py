from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import pytest

from scripts.maintenance import project_process as pm


PID = 7312


def _fake_process(
    proc_root: Path,
    root: Path,
    *,
    service: str = "backend",
    start_ticks: int = 12345,
    executable: Path | None = None,
    argv: tuple[str, ...] | None = None,
    cwd: Path | None = None,
) -> Path:
    proc = proc_root / str(PID)
    proc.mkdir(parents=True, exist_ok=True)
    for child in proc.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            raise AssertionError("unexpected proc fixture directory")
    if service == "backend":
        argv = argv or ("uvicorn", "app.main:app")
    elif service == "worker":
        argv = argv or ("celery", "-A", "app.celery_app", "worker")
    elif service == "frontend":
        argv = argv or ("pnpm", "dev")
    else:
        raise AssertionError(service)
    expected_cwd = pm.expected_cwd(root.resolve(), service)
    expected_cwd.mkdir(parents=True, exist_ok=True)
    cwd = cwd or expected_cwd
    executable = executable or (root / "fake-python")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("fixture executable")
    (proc / "stat").write_text(
        f"{PID} (fake process ) name) "
        + " ".join(
            ["S", "1", str(PID), "0", "0"] + ["0"] * 14 + [str(start_ticks), "0"]
        )
    )
    (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
    (proc / "exe").symlink_to(executable)
    (proc / "cwd").symlink_to(cwd)
    return proc


def _record(root: Path, proc_root: Path, service: str = "backend"):
    return pm.record_process(root, service, PID, proc_root=proc_root, timeout=0)


def test_development_stop_cannot_signal_production_record(tmp_path, monkeypatch):
    production = tmp_path / "production"
    development = tmp_path / "development"
    production.mkdir()
    development.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, production)
    _record(production, proc_root)
    dev_run = development / "run"
    dev_run.mkdir()
    (dev_run / "backend.process.json").write_text(
        pm.identity_path(production, "backend").read_text()
    )
    signalled = []

    with pytest.raises(pm.ProcessIdentityError, match="root mismatch"):
        pm.stop_process(
            development,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda *_: 99,
            signal_sender=signalled.append,
            waiter=lambda *_: True,
        )

    assert signalled == []


def test_development_start_cannot_record_or_adopt_production_process(tmp_path):
    production = tmp_path / "production"
    development = tmp_path / "development"
    production.mkdir()
    development.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, production)

    assert pm.process_status(development, "backend", proc_root=proc_root) is False
    with pytest.raises(pm.ProcessIdentityError, match="outside project root"):
        pm.record_process(development, "backend", PID, proc_root=proc_root, timeout=0)
    assert not pm.identity_path(development, "backend").exists()


def test_pid_reuse_is_rejected_before_pidfd_or_signal(tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    proc_root = tmp_path / "proc"
    proc = _fake_process(proc_root, root)
    _record(root, proc_root)
    _fake_process(proc_root, root, start_ticks=12346)
    pidfd_calls = []
    signal_calls = []

    with pytest.raises(pm.ProcessIdentityError, match="PID .* reused"):
        pm.stop_process(
            root,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda *args: pidfd_calls.append(args),
            signal_sender=signal_calls.append,
        )

    assert pidfd_calls == []
    assert signal_calls == []
    assert proc.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("executable", "executable changed"),
        ("argv", "argv changed"),
        ("cwd", "CWD changed"),
    ],
)
def test_wrong_process_identity_is_rejected(tmp_path, mutation, message):
    root = tmp_path / "development"
    root.mkdir()
    proc_root = tmp_path / "proc"
    proc = _fake_process(proc_root, root)
    _record(root, proc_root)
    if mutation == "executable":
        replacement = root / "other-python"
        replacement.write_text("other executable")
        (proc / "exe").unlink()
        (proc / "exe").symlink_to(replacement)
    elif mutation == "argv":
        (proc / "cmdline").write_bytes(b"uvicorn\0other:app\0")
    else:
        other_cwd = root / "other-cwd"
        other_cwd.mkdir()
        (proc / "cwd").unlink()
        (proc / "cwd").symlink_to(other_cwd)
    signalled = []

    with pytest.raises(pm.ProcessIdentityError, match=message):
        pm.stop_process(
            root,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda *_: 99,
            signal_sender=signalled.append,
            waiter=lambda *_: True,
        )
    assert signalled == []


def test_wrong_source_root_is_rejected(tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    other_root = tmp_path / "copied-checkout"
    other_root.mkdir()
    (other_root / "run").mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, root)
    _record(root, proc_root)
    pm.identity_path(other_root, "backend").write_text(
        pm.identity_path(root, "backend").read_text()
    )
    signalled = []

    with pytest.raises(pm.ProcessIdentityError, match="root mismatch"):
        pm.stop_process(
            other_root,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda *_: 99,
            signal_sender=signalled.append,
        )
    assert signalled == []


def test_stale_legacy_pid_file_cannot_signal_unrelated_process(tmp_path):
    root = tmp_path / "development"
    (root / "run").mkdir(parents=True)
    (root / "run" / "backend.pid").write_text(f"{PID}\n")
    pidfd_calls = []

    with pytest.raises(
        pm.ProcessIdentityError, match="legacy PID file has no identity"
    ):
        pm.stop_all(root)
    with pytest.raises(
        pm.ProcessIdentityError, match="legacy PID file has no identity"
    ):
        pm.process_status(root, "backend")
    with pytest.raises(
        pm.ProcessIdentityError, match="legacy PID file has no identity"
    ):
        pm.stop_process(
            root, "backend", pidfd_open=lambda *args: pidfd_calls.append(args)
        )

    assert pidfd_calls == []


def test_missing_identity_fails_closed(tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    with pytest.raises(pm.ProcessIdentityError, match="identity is missing"):
        pm.stop_process(root, "backend")


def test_port_conflict_is_reported_without_signalling(monkeypatch):
    class ConflictingSocket:
        def __init__(self, *args):
            pass

        def bind(self, address):
            raise OSError(errno.EADDRINUSE, "address in use")

        def close(self):
            pass

    monkeypatch.setattr(
        pm.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))
        ],
    )
    monkeypatch.setattr(pm.socket, "socket", ConflictingSocket)

    with pytest.raises(pm.PortConflict, match="no process was signalled"):
        pm.require_port_available("127.0.0.1", 8080)


def test_stop_uses_pidfd_and_only_sigterm_after_full_validation(tmp_path, monkeypatch):
    root = tmp_path / "development"
    root.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, root)
    _record(root, proc_root)
    closed = []
    signals = []
    monkeypatch.setattr(pm.os, "close", closed.append)

    assert (
        pm.stop_process(
            root,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda pid, flags: 404,
            signal_sender=signals.append,
            waiter=lambda pidfd, timeout: True,
        )
        is True
    )

    assert signals == [404]
    assert closed == [404]
    assert not pm.identity_path(root, "backend").exists()


def test_shutdown_timeout_never_escalates_to_sigkill(tmp_path, monkeypatch):
    root = tmp_path / "development"
    root.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(proc_root, root)
    _record(root, proc_root)
    sent_signals = []
    monkeypatch.setattr(pm.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        pm.signal,
        "pidfd_send_signal",
        lambda pidfd, signum, *_: sent_signals.append((pidfd, signum)),
    )

    with pytest.raises(pm.ProcessIdentityError, match="no SIGKILL sent"):
        pm.stop_process(
            root,
            "backend",
            proc_root=proc_root,
            pidfd_open=lambda *_: 505,
            waiter=lambda *_: False,
        )

    assert sent_signals == [(505, pm.signal.SIGTERM)]
    assert pm.identity_path(root, "backend").exists()


def test_launch_and_stop_scripts_have_no_global_termination():
    repo = Path(__file__).resolve().parents[2]
    start = (repo / "start.sh").read_text()
    stop = (repo / "stop_all.sh").read_text()
    deploy = (repo / "scripts" / "maintenance" / "dogfood_deploy.sh").read_text()
    for text in (start, stop, deploy):
        assert "pkill" not in text
        assert "fuser -k" not in text
        assert "kill -KILL" not in text
        assert "kill -9" not in text
    assert "project_process.py" in start
    assert "project_process.py" in stop
