"""Disposable real-app API tests; no live accounts, services or providers."""

import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import get_password_hash
from app.config import settings
from app.database import get_db
from app.models import User, Project, Task, TaskExecution, PlanningSession
from app.dev_api_only import validate_api_only_settings
from scripts import dev_runtime


@pytest.fixture
def api_only(monkeypatch, tmp_path, db_session_factory):
    from app import main
    from app.services.auth.rate_limit import clear_auth_rate_limits

    monkeypatch.setattr(settings, "DEV_API_ONLY", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "SECRET_KEY", secrets.token_urlsafe(48))
    monkeypatch.setattr(settings, "RUNTIME_ROOT", str(tmp_path / "runtime"))
    database = tmp_path / "database" / "orchestrator-dev.sqlite3"
    database.parent.mkdir()
    database.touch()  # Real requests use only the existing in-memory DB fixture.
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{database}")
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("HOST_WORKSPACE_ROOT", str(workspace))

    def forbidden(*args, **kwargs):
        pytest.fail(
            "startup migration/cleanup/recovery, transport or provider attempted"
        )

    monkeypatch.setattr(main, "init_db", forbidden)
    monkeypatch.setattr(main, "get_db_session", forbidden)
    monkeypatch.setattr(
        main.CheckpointService, "cleanup_orphaned_checkpoints", forbidden
    )
    monkeypatch.setattr(
        main.PlanningSessionService, "recover_active_sessions", forbidden
    )
    monkeypatch.setattr("kombu.Producer.publish", forbidden)
    monkeypatch.setattr("celery.app.base.Celery.send_task", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", forbidden)
    monkeypatch.setattr("redis.Redis.execute_command", forbidden)
    monkeypatch.setattr(
        "app.services.agents.agent_runtime.create_agent_runtime", forbidden
    )
    monkeypatch.setattr(
        "app.services.agents.agent_backends.require_backend_descriptor", forbidden
    )

    password = secrets.token_urlsafe(18)
    with db_session_factory() as db:
        db.add(
            User(
                email="eval@local.dev",
                hashed_password=get_password_hash(password),
                is_active=True,
            )
        )
        db.commit()

    def override_db():
        with db_session_factory() as db:
            yield db

    main.app.dependency_overrides[get_db] = override_db
    clear_auth_rate_limits()
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client, password=password, db=db_session_factory
            )
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        clear_auth_rate_limits()


def login(api_only):
    response = api_only.client.post(
        "/api/v1/auth/session/login",
        json={"email": "eval@local.dev", "password": api_only.password},
    )
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()


def test_real_auth_project_task_creation_without_execution(api_only):
    client = api_only.client
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/projects", json={"name": "Denied"}).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/session/login",
            json={"email": "eval@local.dev", "password": "wrong"},
        ).status_code
        == 401
    )
    login(api_only)
    assert client.get("/api/v1/auth/me").json()["email"] == "eval@local.dev"
    project = client.post("/api/v1/projects", json={"name": "API only project"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": "Pending only",
            "description": "No execution",
        },
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    assert task.json()["status"] == "pending"
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "pending"
    assert client.get(f"/api/v1/projects/{project_id}/tasks").json()[0]["id"] == task_id
    assert (
        client.get(f"/api/v1/projects/{project_id}/sessions?page=1").status_code == 200
    )
    assert client.get("/api/v1/dashboard/attention").status_code == 200
    with api_only.db() as db:
        assert db.query(Task).count() == 1
        assert db.query(TaskExecution).count() == 0
    assert client.get("/health").json()["execution_enabled"] is False
    assert client.post("/api/v1/auth/session/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/tasks/1/execute"),
        ("POST", "/api/v1/tasks/1/retry"),
        ("POST", "/api/v1/tasks/1/change-set/accept"),
        ("POST", "/api/v1/sessions"),
        ("POST", "/api/v1/sessions/1/start"),
        ("POST", "/api/v1/sessions/1/resume"),
        ("POST", "/api/v1/planning/sessions"),
        ("POST", "/api/v1/projects/1/baseline/rebuild"),
        ("POST", "/api/v1/mobile/tasks"),
        ("POST", "/auth/session/login"),
        ("PUT", "/api/v1/tasks/1"),
        ("GET", "/api/v1/auth/generate-keypair"),
        ("POST", "/api/v1/auth/register"),
    ],
)
def test_alternate_surfaces_fail_closed_before_route_work(api_only, method, path):
    login(api_only)
    response = api_only.client.request(method, path, json={})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DEVELOPMENT_EXECUTION_DISABLED"
    with api_only.db() as db:
        assert db.query(TaskExecution).count() == 0


def test_websocket_is_disabled(api_only):
    with pytest.raises(WebSocketDisconnect) as exc:
        with api_only.client.websocket_connect("/api/v1/sessions/1/status"):
            pytest.fail("WebSocket accepted")
    assert exc.value.code == 1008


def test_allowed_auth_preflight_and_disabled_execution_preflight(api_only):
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    assert (
        api_only.client.options(
            "/api/v1/auth/session/login", headers=headers
        ).status_code
        == 200
    )
    assert (
        api_only.client.options("/api/v1/tasks/1/execute", headers=headers).status_code
        == 503
    )


def test_signing_key_is_still_mandatory(api_only, monkeypatch):
    from app.config import validate_runtime_secrets

    monkeypatch.setattr(settings, "SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="SECRET_KEY is unset"):
        validate_runtime_secrets()


def test_default_mode_keeps_existing_celery_submission(api_only, monkeypatch):
    from app.celery_app import celery_app

    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    sentinel = object()
    monkeypatch.setattr("celery.app.base.Celery.send_task", lambda *a, **k: sentinel)
    assert celery_app.send_task("fixture_only_not_sent") is sentinel


def test_no_resume_of_preexisting_planning(api_only):
    from app.main import app

    with api_only.db() as db:
        project = Project(name="Abandoned", user_id=1, workspace_path="abandoned")
        db.add(project)
        db.flush()
        db.add(
            PlanningSession(
                project_id=project.id, title="Old", prompt="do not run", status="active"
            )
        )
        db.commit()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    with api_only.db() as db:
        assert db.query(PlanningSession).one().status == "active"
        assert db.query(TaskExecution).count() == 0


def test_project_path_traversal_refused_before_insert(api_only):
    login(api_only)
    response = api_only.client.post(
        "/api/v1/projects", json={"name": "Escape", "workspace_path": "../../escape"}
    )
    assert response.status_code == 422
    with api_only.db() as db:
        assert db.query(Project).count() == 0


@pytest.mark.parametrize("eager", [False, True])
def test_celery_submission_and_inline_execution_refused(api_only, monkeypatch, eager):
    from app.celery_app import celery_app

    monkeypatch.setitem(celery_app.conf, "task_always_eager", eager)

    # A local test task is never executed, even if eager execution was enabled.
    def never_execute():
        pytest.fail("executed")

    task = celery_app.task(name="p42_v9_never_execute", shared=False, lazy=False)(
        never_execute
    )
    try:
        for submit in (lambda: celery_app.send_task(task.name), task.delay, task.apply):
            with pytest.raises(RuntimeError, match="DEVELOPMENT_EXECUTION_DISABLED"):
                submit()
    finally:
        celery_app.tasks.pop(task.name, None)


def test_direct_production_and_missing_database_configuration_refused(
    api_only, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="ENVIRONMENT"):
        validate_api_only_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    # Lexical guard runs before attempting to open/stat production contents.
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "sqlite:////root/.openclaw/workspace/vault/orchestrator-production-runtime/no-open.sqlite3",
    )
    with pytest.raises(RuntimeError, match="production paths"):
        validate_api_only_settings()
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        f"sqlite:///{tmp_path}/database/orchestrator-dev.sqlite3",
    )
    Path(tmp_path / "database" / "orchestrator-dev.sqlite3").unlink()
    with pytest.raises(RuntimeError, match="never initializes"):
        validate_api_only_settings()


def test_api_only_launcher_never_bootstraps_services(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo)
    with pytest.raises(dev_runtime.RuntimeToolError):
        dev_runtime.run_api_only(config)
    assert not config.root.exists()
    # Fixture-only owned directories, not persistent development initialization.
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    config.database_file.touch()
    calls = []
    monkeypatch.setattr(
        dev_runtime.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=0),
    )
    assert dev_runtime.run_api_only(config) == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:4] == ["-m", "uvicorn", "app.main:app"]
    assert kwargs["env"]["DEV_API_ONLY"] == "true"
    assert kwargs["env"]["CELERY_BROKER_URL"] == "redis://127.0.0.1:16379/0"
    assert not (config.root / "logs" / "worker.log").exists()
    monkeypatch.setattr(
        dev_runtime, "_load_state", lambda *args: {"processes": {"worker": {}}}
    )
    monkeypatch.setattr(
        dev_runtime, "_process_identity", lambda record: ("unknown", "fixture")
    )
    with pytest.raises(
        dev_runtime.RuntimeToolError, match="recorded runtime processes stopped"
    ):
        dev_runtime.run_api_only(config)
    assert len(calls) == 1
