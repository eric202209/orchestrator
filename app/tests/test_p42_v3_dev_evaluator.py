"""Offline evaluator initialization; all database writes use temporary fixtures."""

import secrets
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_runtime

REAL_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "app").symlink_to(REAL_ROOT / "app", target_is_directory=True)
    monkeypatch.setattr(dev_runtime, "__file__", str(repo / "scripts/dev_runtime.py"))
    return dev_runtime.RuntimeConfig.for_repo(repo)


def test_explicit_initialization_and_browser_session(config, capsys):
    password = secrets.token_urlsafe(18)
    result = dev_runtime.initialize_evaluator(config, password)
    assert result["status"] == "evaluator_initialized"
    assert capsys.readouterr() == ("", "")
    with sqlite3.connect(config.database_file) as db:
        row = db.execute(
            "SELECT email, hashed_password, is_active FROM users"
        ).fetchone()
        assert row[0] == dev_runtime.EVALUATOR_EMAIL and row[2] == 1
        assert password not in row[1]
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert config.database_file.stat().st_mode & 0o777 == 0o600
    # Exercise the unchanged browser login function with this temporary DB.
    code = """
import sys
from starlette.requests import Request
from starlette.responses import Response
from app.database import SessionLocal
from app.api.v1.endpoints import auth
from app.schemas.auth import UserLogin
from app.config import settings
auth.enforce_auth_rate_limit = lambda *args: None
with SessionLocal() as db:
    response = Response()
    user = auth.session_login(UserLogin(email='eval@local.dev', password=sys.stdin.read()), Request({'type':'http'}), response, db)
    assert user.email == 'eval@local.dev'
    assert settings.SESSION_COOKIE_NAME in response.headers['set-cookie']
    assert 'httponly' in response.headers['set-cookie'].lower()
"""
    checked = subprocess.run(
        [sys.executable, "-c", code],
        cwd=config.process_cwd,
        env=dev_runtime.build_environment(config),
        input=password,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
    with pytest.raises(dev_runtime.RuntimeToolError, match="already exists"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    # No password, hash or token is placed in runtime metadata/log files.
    for directory in ("logs", "run"):
        for path in (config.root / directory).rglob("*"):
            if path.is_file():
                assert password.encode() not in path.read_bytes()
    for path in config.root.glob("*.json"):
        assert password not in path.read_text()
        assert row[1] not in path.read_text()


def test_foreign_checkout_and_production_root_refused(config, tmp_path):
    foreign = dev_runtime.RuntimeConfig.for_repo(tmp_path / "orchestrator-production")
    with pytest.raises(dev_runtime.RuntimeToolError, match="development checkout"):
        dev_runtime.initialize_evaluator(foreign, secrets.token_urlsafe(18))
    substituted = dev_runtime.RuntimeConfig(
        repo_root=config.repo_root, root=tmp_path / "orchestrator-production-runtime"
    )
    with pytest.raises(dev_runtime.RuntimeToolError, match="canonical trusted"):
        dev_runtime.initialize_evaluator(substituted, secrets.token_urlsafe(18))
    assert not foreign.root.exists() and not substituted.root.exists()


def test_symlinked_database_refused(config, tmp_path):
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    protected = tmp_path / "protected.sqlite"
    protected.write_bytes(b"unchanged")
    config.database_file.symlink_to(protected)
    with pytest.raises(dev_runtime.RuntimeToolError, match="symlink"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert protected.read_bytes() == b"unchanged"


def test_running_or_unknown_process_refused(config, monkeypatch):
    owner = dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    state = dev_runtime._load_state(config, owner)
    state["processes"] = {"api": {"fixture": True}}
    dev_runtime._save_state(config, state)
    monkeypatch.setattr(
        dev_runtime, "_process_identity", lambda record: ("unknown", "fixture")
    )
    with pytest.raises(dev_runtime.RuntimeToolError, match="stopped runtime"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert not config.database_file.exists()


def test_init_requires_terminal_and_never_echoes_password(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert dev_runtime.main(["init-evaluator"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_short_password_refused_without_creating_runtime(config):
    with pytest.raises(dev_runtime.RuntimeToolError, match="at least 8"):
        dev_runtime.initialize_evaluator(config, "")
    assert not config.root.exists()


def test_worksheet_wrapper_uses_exact_isolated_environment(config, monkeypatch):
    import json
    from types import SimpleNamespace

    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    launcher = config.repo_root / "venv/bin/celery"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{sys.executable}\n")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    monkeypatch.setenv("SECRET_KEY", "must-not-be-inherited")
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        assert kwargs["cwd"] == config.process_cwd
        assert kwargs["env"]["DATABASE_URL"] == f"sqlite:///{config.database_file}"
        assert kwargs["env"]["SECRET_KEY"] != "must-not-be-inherited"
        value = json.loads(kwargs["input"])
        context = value["context"]
        assert [
            context["script"],
            *context["args"],
        ] == dev_runtime.development_worker_command(config)
        assert context["cwd"] == str(config.process_cwd)
        assert context["interpreter"] == sys.executable
        assert context["worker_node_identity"] == config.worker_name
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"approval_state": "NOT_APPROVED"})
        )

    monkeypatch.setattr(dev_runtime.subprocess, "run", run)
    assert (
        dev_runtime.worker_worksheet(config, "a" * 40, "b" * 40)["approval_state"]
        == "NOT_APPROVED"
    )
    assert len(seen) == 1


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.xfail(
    strict=True,
    reason="P42-V4-R1 F1 blocked: same-UID filesystem boundary is not corrected",
)
def test_evaluator_refuses_hardlinked_sqlite_files(config, tmp_path, suffix):
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    outside = tmp_path / "outside.sqlite3"
    with sqlite3.connect(outside) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
    before = outside.read_bytes()
    before_mode = outside.stat().st_mode
    os.link(outside, Path(str(config.database_file) + suffix))
    with pytest.raises(dev_runtime.RuntimeToolError, match="unsafe SQLite"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert outside.read_bytes() == before
    assert outside.stat().st_mode == before_mode


@pytest.mark.xfail(
    strict=True,
    reason="P42-V4-R1 F1 blocked: SQLite pathname reopen race is not corrected",
)
def test_database_substituted_at_subprocess_boundary_is_refused(
    config, tmp_path, monkeypatch
):
    outside = tmp_path / "outside.sqlite3"
    with sqlite3.connect(outside) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
    before = outside.read_bytes()
    before_mode = outside.stat().st_mode
    real_run = subprocess.run
    replaced = False

    def run(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            config.database_file.unlink(missing_ok=True)
            config.database_file.symlink_to(outside)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dev_runtime.subprocess, "run", run)
    with pytest.raises(dev_runtime.RuntimeToolError):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert replaced
    assert outside.read_bytes() == before
    assert outside.stat().st_mode == before_mode


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_evaluator_refuses_symlinked_sqlite_sidecar(config, tmp_path, suffix):
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"outside-runtime sentinel")
    Path(str(config.database_file) + suffix).symlink_to(outside)
    with pytest.raises(dev_runtime.RuntimeToolError, match="symlink"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert outside.read_bytes() == b"outside-runtime sentinel"


def test_substituted_database_environment_refused(config, tmp_path, monkeypatch):
    outside = tmp_path / "outside.sqlite3"
    env = dev_runtime.build_environment(config)
    env["DATABASE_URL"] = f"sqlite:///{outside}"
    monkeypatch.setattr(dev_runtime, "build_environment", lambda config: env)
    with pytest.raises(dev_runtime.RuntimeToolError, match="DATABASE_URL"):
        dev_runtime.initialize_evaluator(config, secrets.token_urlsafe(18))
    assert not outside.exists()
    assert not config.root.exists()


@pytest.mark.parametrize("failure_prompt", [1, 2, None])
def test_real_getpass_echo_control_and_no_fallback_read(
    config, monkeypatch, capsys, failure_prompt
):
    import pty
    import termios

    password = secrets.token_urlsafe(18)
    master, slave = pty.openpty()
    real_open = os.open
    real_tcsetattr = termios.tcsetattr
    original = termios.tcgetattr(slave)
    prompts = 0
    reads = []
    initialized = []

    def open_tty(path, flags, *args, **kwargs):
        if path == "/dev/tty":
            return os.dup(slave)
        return real_open(path, flags, *args, **kwargs)

    def control_echo(fd, when, attributes):
        nonlocal prompts
        if not attributes[3] & termios.ECHO:
            prompts += 1
            if prompts == failure_prompt:
                raise termios.error("synthetic echo-control failure")
        return real_tcsetattr(fd, when, attributes)

    def hidden_read(prompt, stream=None, input=None):
        # This is the real getpass terminal path, with only keystrokes stubbed.
        # The ordinary fallback has echo on and must never reach this read.
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        reads.append(prompt)
        return password

    def initialize(*args):
        initialized.append(args)
        return {"status": "fixture-only"}

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(dev_runtime, "_default_config", lambda: config)
    monkeypatch.setattr(os, "open", open_tty)
    monkeypatch.setattr(termios, "tcsetattr", control_echo)
    monkeypatch.setattr(
        dev_runtime.getpass, "getpass", dev_runtime.getpass.unix_getpass
    )
    monkeypatch.setattr(dev_runtime.getpass, "_raw_input", hidden_read)
    monkeypatch.setattr(dev_runtime, "initialize_evaluator", initialize)
    try:
        result = dev_runtime.main(["init-evaluator"])
        assert termios.tcgetattr(slave) == original
        if failure_prompt is None:
            assert result == 0 and len(reads) == 2 and len(initialized) == 1
        else:
            assert result == 1 and len(reads) == failure_prompt - 1
            assert not initialized
        assert not config.root.exists()
        output = capsys.readouterr()
        assert password not in output.out + output.err
        assert "synthetic echo-control failure" not in output.err
    finally:
        os.close(master)
        os.close(slave)


def test_password_confirmation_mismatch_has_no_mutation(config, monkeypatch, capsys):
    password = secrets.token_urlsafe(18)
    confirmation = secrets.token_urlsafe(18)
    values = iter([password, confirmation])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(dev_runtime, "_default_config", lambda: config)
    monkeypatch.setattr(dev_runtime.getpass, "getpass", lambda prompt: next(values))
    assert dev_runtime.main(["init-evaluator"]) == 1
    assert not config.root.exists()
    output = capsys.readouterr()
    assert "confirmation did not match" in output.err
    assert password not in output.out + output.err
    assert confirmation not in output.out + output.err


def test_echo_warning_aborts_before_any_initialization(config, monkeypatch, capsys):
    import warnings

    password = secrets.token_urlsafe(18)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(dev_runtime, "_default_config", lambda: config)

    def unsafe_getpass(prompt):
        warnings.warn("Cannot disable echo", dev_runtime.getpass.GetPassWarning)
        return password

    monkeypatch.setattr(dev_runtime.getpass, "getpass", unsafe_getpass)
    assert dev_runtime.main(["init-evaluator"]) == 1
    assert not config.root.exists()
    output = capsys.readouterr()
    assert password not in output.out + output.err
