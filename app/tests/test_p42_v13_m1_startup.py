"""P42-V13 disposable API-only startup contract tests."""

import sqlite3
import socket

import pytest

from scripts import dev_runtime


@pytest.fixture
def runtime_config(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo)
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    config.database_file.touch()
    (repo / "frontend" / "node_modules").mkdir(parents=True)
    return config


def _create_state_tables(connection):
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, description TEXT, applied_at TEXT);
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, is_active BOOLEAN, hashed_password TEXT);
        CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT, is_active BOOLEAN);
        CREATE TABLE planning_sessions (id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT);
        CREATE TABLE task_executions (id INTEGER PRIMARY KEY, session_id INTEGER, task_id INTEGER, status TEXT, dispatch_state TEXT);
        """
    )


def test_api_only_environment_routes_vite_to_the_foreground_api(runtime_config):
    env = dev_runtime.build_environment(runtime_config)

    assert env["ORCHESTRATOR_DEV_API_TARGET"] == runtime_config.api_url


def test_api_only_state_requires_the_existing_evaluator_account(runtime_config):
    with sqlite3.connect(runtime_config.database_file) as connection:
        _create_state_tables(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('test', 'fixture', 'now')"
        )

    with pytest.raises(dev_runtime.RuntimeToolError, match="evaluator account"):
        dev_runtime.validate_api_only_state(runtime_config)


def test_api_only_state_rejects_active_execution_before_start(runtime_config):
    with sqlite3.connect(runtime_config.database_file) as connection:
        _create_state_tables(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('test', 'fixture', 'now')"
        )
        connection.execute(
            "INSERT INTO users VALUES (1, 'eval@local.dev', 1, 'fixture')"
        )
        connection.execute(
            "INSERT INTO task_executions VALUES (1, 1, 1, 'running', 'READY')"
        )

    with pytest.raises(dev_runtime.RuntimeToolError, match="active execution"):
        dev_runtime.validate_api_only_state(runtime_config)


def test_api_only_frontend_command_is_server_only_and_loopback_targeted(
    runtime_config, monkeypatch
):
    monkeypatch.setattr(dev_runtime, "_which_required", lambda name: f"/bin/{name}")

    command, cwd = dev_runtime.api_only_frontend_command(runtime_config)

    assert command == ["/bin/pnpm", "dev"]
    assert cwd == runtime_config.repo_root / "frontend"


def test_port_probe_allows_immediate_reuse_after_listener_shutdown():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _address = server.accept()
    client.close()
    accepted.close()
    server.close()

    assert dev_runtime._port_available(port)
