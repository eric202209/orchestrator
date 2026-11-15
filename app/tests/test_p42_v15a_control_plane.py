import asyncio

from app.config import settings
from app.dev_api_only import DevelopmentAPIOnlyMiddleware, _is_control_plane_write


def test_execution_disabled_control_plane_write_allowlist():
    assert _is_control_plane_write("POST", "/api/v1/projects")
    assert _is_control_plane_write("PUT", "/api/v1/projects/12")
    assert _is_control_plane_write("DELETE", "/api/v1/tasks/18")
    assert _is_control_plane_write("POST", "/api/v1/auth/session/login")
    assert _is_control_plane_write("PATCH", "/api/v1/settings/system")

    assert not _is_control_plane_write("POST", "/api/v1/tasks/18/retry")
    assert not _is_control_plane_write("POST", "/api/v1/tasks/18/execute")
    assert not _is_control_plane_write("POST", "/api/v1/sessions")
    assert not _is_control_plane_write("POST", "/api/v1/planning/sessions")


def test_execution_disabled_middleware_rejects_dispatch_before_inner_app(monkeypatch):
    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", False)
    called = []

    async def inner(scope, receive, send):
        called.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def invoke(method, path):
        messages = []
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "http_version": "1.1",
            "scheme": "http",
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await DevelopmentAPIOnlyMiddleware(inner)(scope, receive, send)
        return messages

    denied = asyncio.run(invoke("POST", "/api/v1/tasks/18/retry"))
    assert denied[0]["status"] == 503
    assert b"EXECUTION_DISABLED" in denied[1]["body"]
    assert called == []

    allowed = asyncio.run(invoke("PUT", "/api/v1/projects/12"))
    assert allowed[0]["status"] == 200

    settings_allowed = asyncio.run(invoke("PATCH", "/api/v1/settings/system"))
    assert settings_allowed[0]["status"] == 200
    assert called == ["/api/v1/projects/12", "/api/v1/settings/system"]


def test_celery_dispatch_refuses_execution_disabled(monkeypatch):
    from app.celery_app import _require_execution_enabled

    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", False)
    try:
        _require_execution_enabled()
    except RuntimeError as exc:
        assert str(exc) == "EXECUTION_DISABLED"
    else:
        raise AssertionError("execution-disabled Celery dispatch was not refused")


def test_execution_disabled_lifespan_skips_initialization_and_recovery(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", False)
    monkeypatch.setattr(main, "validate_runtime_secrets", lambda: None)
    calls = []
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))

    async def exercise_lifespan():
        async with main.lifespan(main.app):
            pass

    asyncio.run(exercise_lifespan())
    assert calls == []
