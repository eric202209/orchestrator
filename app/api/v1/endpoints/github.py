"""GitHub API endpoints."""

import json
import hashlib
import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import settings
from app.dependencies import get_current_active_user
from app.models import User
from app.services.github_service import GitHubService
from app.tasks.github_tasks import (
    process_github_issue_event,
    process_github_pr_event,
    process_github_push_event,
)

router = APIRouter()


def _verify_webhook_signature(
    body: bytes, signature: str | None, payload: Optional[dict] = None
) -> None:
    """Validate the GitHub webhook signature when a secret is configured."""
    if not settings.GITHUB_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503, detail="GitHub webhook secret is not configured"
        )

    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing GitHub webhook signature")

    provided = signature.split("=", 1)[1]
    secret = settings.GITHUB_WEBHOOK_SECRET.encode("utf-8")

    candidate_bodies = [body]
    if payload is not None:
        candidate_bodies.extend(
            [
                json.dumps(payload).encode("utf-8"),
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            ]
        )

    if not any(
        hmac.compare_digest(
            hmac.new(secret, candidate_body, hashlib.sha256).hexdigest(),
            provided,
        )
        for candidate_body in candidate_bodies
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")


def _extract_repo(payload: dict) -> tuple[str, str]:
    repository = payload.get("repository") or {}
    owner = (
        repository.get("owner", {}).get("login")
        or repository.get("owner", {}).get("name")
        or repository.get("full_name", "/").split("/", 1)[0]
    )
    repo = repository.get("name")
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="Missing repository information")
    return owner, repo


@router.post("/github/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
):
    """Handle real GitHub webhooks and route them to background tasks."""
    body = await request.body()

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    _verify_webhook_signature(body, x_hub_signature_256, payload)

    event_type = x_github_event or payload.get("type", "unknown")
    owner, repo = _extract_repo(payload)

    if event_type == "push":
        branch = (payload.get("ref") or "").removeprefix("refs/heads/") or "main"
        task = process_github_push_event.delay(payload, owner, repo, branch)
        return {
            "status": "queued",
            "event": event_type,
            "task_id": task.id,
            "repository": f"{owner}/{repo}",
            "branch": branch,
        }

    if event_type == "pull_request":
        pr_number = (payload.get("pull_request") or {}).get("number") or payload.get(
            "number"
        )
        if not pr_number:
            raise HTTPException(status_code=400, detail="Missing pull request number")
        task = process_github_pr_event.delay(payload, owner, repo, int(pr_number))
        return {
            "status": "queued",
            "event": event_type,
            "task_id": task.id,
            "repository": f"{owner}/{repo}",
            "pull_request": int(pr_number),
        }

    if event_type == "issues":
        issue_number = (payload.get("issue") or {}).get("number") or payload.get(
            "number"
        )
        if not issue_number:
            raise HTTPException(status_code=400, detail="Missing issue number")
        task = process_github_issue_event.delay(payload, owner, repo, int(issue_number))
        return {
            "status": "queued",
            "event": event_type,
            "task_id": task.id,
            "repository": f"{owner}/{repo}",
            "issue": int(issue_number),
        }

    return {
        "status": "ignored",
        "event": event_type,
        "repository": f"{owner}/{repo}",
    }


@router.get("/github/repos/{owner}/{repo}")
async def get_repo_info(owner: str, repo: str):
    """Get repository information from GitHub"""
    try:
        service = GitHubService()
        return await service.get_repository(owner, repo)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 500
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.post("/github/create-issue")
async def create_github_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: Optional[list] = None,
    current_user: User = Depends(get_current_active_user),
):
    """Create a GitHub issue"""
    try:
        service = GitHubService()
        return await service.create_issue(owner, repo, title, body, labels=labels)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
