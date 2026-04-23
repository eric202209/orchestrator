"""Regression tests for GitHub endpoint routing."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.api.v1.endpoints import github as github_endpoint


client = TestClient(app)


class _FakeAsyncResult:
    def __init__(self, task_id: str):
        self.id = task_id


def _signed_headers(event: str, payload: dict, secret: str) -> dict[str, str]:
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def test_push_webhook_routes_to_push_task(monkeypatch):
    settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    captured = {}

    def fake_delay(payload, owner, repo, branch):
        captured["payload"] = payload
        captured["owner"] = owner
        captured["repo"] = repo
        captured["branch"] = branch
        return _FakeAsyncResult("push-task-1")

    monkeypatch.setattr(github_endpoint.process_github_push_event, "delay", fake_delay)

    payload = {
        "ref": "refs/heads/main",
        "repository": {
            "name": "clawmobile",
            "owner": {"login": "Openclaw"},
        },
    }

    response = client.post(
        "/api/v1/github/webhook",
        headers=_signed_headers("push", payload, settings.GITHUB_WEBHOOK_SECRET),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "queued",
        "event": "push",
        "task_id": "push-task-1",
        "repository": "Openclaw/clawmobile",
        "branch": "main",
    }
    assert captured["owner"] == "Openclaw"
    assert captured["repo"] == "clawmobile"
    assert captured["branch"] == "main"


def test_pull_request_webhook_routes_to_pr_task(monkeypatch):
    settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    captured = {}

    def fake_delay(payload, owner, repo, pr_number):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["pr_number"] = pr_number
        return _FakeAsyncResult("pr-task-1")

    monkeypatch.setattr(github_endpoint.process_github_pr_event, "delay", fake_delay)

    payload = {
        "number": 42,
        "repository": {
            "name": "clawmobile",
            "owner": {"login": "Openclaw"},
        },
        "pull_request": {"number": 42},
    }
    response = client.post(
        "/api/v1/github/webhook",
        headers=_signed_headers(
            "pull_request", payload, settings.GITHUB_WEBHOOK_SECRET
        ),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["pull_request"] == 42
    assert captured == {
        "owner": "Openclaw",
        "repo": "clawmobile",
        "pr_number": 42,
    }


def test_issue_webhook_routes_to_issue_task(monkeypatch):
    settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    captured = {}

    def fake_delay(payload, owner, repo, issue_number):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["issue_number"] = issue_number
        return _FakeAsyncResult("issue-task-1")

    monkeypatch.setattr(github_endpoint.process_github_issue_event, "delay", fake_delay)

    payload = {
        "number": 9,
        "repository": {
            "name": "clawmobile",
            "owner": {"login": "Openclaw"},
        },
        "issue": {"number": 9},
    }
    response = client.post(
        "/api/v1/github/webhook",
        headers=_signed_headers("issues", payload, settings.GITHUB_WEBHOOK_SECRET),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["issue"] == 9
    assert captured == {
        "owner": "Openclaw",
        "repo": "clawmobile",
        "issue_number": 9,
    }


def test_webhook_requires_repository_information():
    settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    payload = {"ref": "refs/heads/main"}
    response = client.post(
        "/api/v1/github/webhook",
        headers=_signed_headers("push", payload, settings.GITHUB_WEBHOOK_SECRET),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing repository information"


def test_webhook_requires_signature_when_secret_configured():
    settings.GITHUB_WEBHOOK_SECRET = "test-secret"

    response = client.post(
        "/api/v1/github/webhook",
        headers={"X-GitHub-Event": "push"},
        json={
            "ref": "refs/heads/main",
            "repository": {"name": "clawmobile", "owner": {"login": "Openclaw"}},
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing GitHub webhook signature"


def test_unauthenticated_create_issue_is_rejected():
    response = client.post(
        "/api/v1/github/create-issue",
        params={
            "owner": "Openclaw",
            "repo": "clawmobile",
            "title": "Regression",
            "body": "Should fail without auth",
        },
    )

    assert response.status_code == 401
