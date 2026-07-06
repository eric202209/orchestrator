"""Tests for broader API rate limiting on write/mutation endpoints."""

from __future__ import annotations

import pytest

from app.services.auth.rate_limit import clear_auth_rate_limits, enforce_api_rate_limit
from fastapi import HTTPException


class _FakeRequest:
    """Minimal stand-in for fastapi.Request with a fixed client IP."""

    def __init__(self, ip: str = "10.0.0.1"):
        self.headers: dict = {}
        self.client = type("C", (), {"host": ip})()


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


def setup_function():
    clear_auth_rate_limits()


def test_api_rate_limit_allows_requests_within_limit(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)

    req = _FakeRequest()
    for _ in range(5):
        enforce_api_rate_limit(req, "session_start")


def test_api_rate_limit_rejects_on_excess(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)

    req = _FakeRequest()
    for _ in range(3):
        enforce_api_rate_limit(req, "task_run")

    with pytest.raises(HTTPException) as exc_info:
        enforce_api_rate_limit(req, "task_run")

    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_api_rate_limit_buckets_are_per_action(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)

    req = _FakeRequest()
    enforce_api_rate_limit(req, "session_start")
    enforce_api_rate_limit(req, "session_start")
    enforce_api_rate_limit(req, "task_run")
    enforce_api_rate_limit(req, "task_run")

    with pytest.raises(HTTPException):
        enforce_api_rate_limit(req, "session_start")


def test_api_rate_limit_buckets_are_per_ip(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)

    req_a = _FakeRequest(ip="1.1.1.1")
    req_b = _FakeRequest(ip="2.2.2.2")

    enforce_api_rate_limit(req_a, "session_start")
    enforce_api_rate_limit(req_a, "session_start")
    # Different IP should be unaffected
    enforce_api_rate_limit(req_b, "session_start")
    enforce_api_rate_limit(req_b, "session_start")


def test_api_rate_limit_buckets_are_per_user_when_authenticated(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)

    req = _FakeRequest(ip="1.1.1.1")
    user_a = _FakeUser(1)
    user_b = _FakeUser(2)

    enforce_api_rate_limit(req, "session_start", current_user=user_a)
    enforce_api_rate_limit(req, "session_start", current_user=user_a)

    # Same IP, different authenticated user should get a separate bucket.
    enforce_api_rate_limit(req, "session_start", current_user=user_b)
    enforce_api_rate_limit(req, "session_start", current_user=user_b)


def test_api_rate_limit_disabled_when_max_attempts_zero(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 0)

    req = _FakeRequest()
    for _ in range(100):
        enforce_api_rate_limit(req, "session_start")


def test_session_start_endpoint_returns_429_when_rate_limited(
    authenticated_client, monkeypatch
):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(cfg.settings, "API_RATE_LIMIT_WINDOW_SECONDS", 60)
    clear_auth_rate_limits()

    authenticated_client.post("/api/v1/sessions/99999/start")
    resp = authenticated_client.post("/api/v1/sessions/99999/start")
    assert resp.status_code == 429
