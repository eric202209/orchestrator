"""GitHub API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import settings
from typing import Optional
import httpx

router = APIRouter()


@router.post("/github/webhook")
async def github_webhook(payload: dict):
    """Handle GitHub webhooks for repository events"""
    # Verify webhook secret (implement your signing logic)
    # For now, just log the event
    
    event_type = payload.get("type", "Unknown")
    repo = payload.get("repository", {}).get("full_name", "Unknown")
    
    # Process different event types
    if event_type == "PushEvent":
        # Handle push events - could trigger task creation
        pass
    elif event_type == "PullRequestEvent":
        # Handle PR events
        pass
    
    return {"status": "received"}


@router.get("/github/repos/{owner}/{repo}")
async def get_repo_info(owner: str, repo: str, db: Session = Depends(get_db)):
    """Get repository information from GitHub"""
    if not settings.GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GitHub token not configured")
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={
                "Authorization": f"token {settings.GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Repository not found")
        elif response.status_code != 200:
            raise HTTPException(status_code=500, detail="GitHub API error")
        
        return response.json()


@router.post("/github/create-issue")
async def create_github_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: Optional[list] = None,
    db: Session = Depends(get_db)
):
    """Create a GitHub issue"""
    if not settings.GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GitHub token not configured")
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            json={
                "title": title,
                "body": body,
                "labels": labels or []
            },
            headers={
                "Authorization": f"token {settings.GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Failed to create issue")
        
        return response.json()
