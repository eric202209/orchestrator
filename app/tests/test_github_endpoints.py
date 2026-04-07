"""Regression tests for GitHub endpoint routing."""

from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.endpoints import github as github_endpoint


client = TestClient(app)


class _FakeAsyncResult:
    def __init__(self, task_id: str):
        self.id = task_id


def test_push_webhook_routes_to_push_task(monkeypatch):
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
        headers={"X-GitHub-Event": "push"},
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
    captured = {}

    def fake_delay(payload, owner, repo, pr_number):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["pr_number"] = pr_number
        return _FakeAsyncResult("pr-task-1")

    monkeypatch.setattr(github_endpoint.process_github_pr_event, "delay", fake_delay)

    response = client.post(
        "/api/v1/github/webhook",
        headers={"X-GitHub-Event": "pull_request"},
        json={
            "number": 42,
            "repository": {
                "name": "clawmobile",
                "owner": {"login": "Openclaw"},
            },
            "pull_request": {"number": 42},
        },
    )

    assert response.status_code == 200
    assert response.json()["pull_request"] == 42
    assert captured == {
        "owner": "Openclaw",
        "repo": "clawmobile",
        "pr_number": 42,
    }


def test_issue_webhook_routes_to_issue_task(monkeypatch):
    captured = {}

    def fake_delay(payload, owner, repo, issue_number):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["issue_number"] = issue_number
        return _FakeAsyncResult("issue-task-1")

    monkeypatch.setattr(github_endpoint.process_github_issue_event, "delay", fake_delay)

    response = client.post(
        "/api/v1/github/webhook",
        headers={"X-GitHub-Event": "issues"},
        json={
            "number": 9,
            "repository": {
                "name": "clawmobile",
                "owner": {"login": "Openclaw"},
            },
            "issue": {"number": 9},
        },
    )

    assert response.status_code == 200
    assert response.json()["issue"] == 9
    assert captured == {
        "owner": "Openclaw",
        "repo": "clawmobile",
        "issue_number": 9,
    }


def test_webhook_requires_repository_information():
    response = client.post(
        "/api/v1/github/webhook",
        headers={"X-GitHub-Event": "push"},
        json={"ref": "refs/heads/main"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing repository information"
