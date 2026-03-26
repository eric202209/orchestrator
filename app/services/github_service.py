"""GitHub service - Integration with GitHub API"""

from typing import Optional, List, Dict, Any
import httpx
from app.config import settings


class GitHubService:
    """Service for GitHub API operations"""
    
    def __init__(self):
        if not settings.GITHUB_TOKEN:
            raise ValueError("GitHub token not configured")
        
        self.headers = {
            "Authorization": f"token {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
    
    async def get_repository(self, owner: str, repo: str) -> Dict[str, Any]:
        """Get repository information"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=self.headers
            )
            
            if response.status_code == 404:
                raise ValueError(f"Repository {owner}/{repo} not found")
            elif response.status_code != 200:
                raise ValueError(f"GitHub API error: {response.status_code}")
            
            return response.json()
    
    async def create_issue(
        self, 
        owner: str, 
        repo: str, 
        title: str, 
        body: str,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Create a GitHub issue"""
        async with httpx.AsyncClient() as client:
            data = {
                "title": title,
                "body": body,
                "labels": labels or [],
                "assignees": assignees or []
            }
            
            response = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                headers=self.headers,
                json=data
            )
            
            if response.status_code not in [200, 201]:
                raise ValueError(f"Failed to create issue: {response.status_code}")
            
            return response.json()
    
    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main"
    ) -> Dict[str, Any]:
        """Create a GitHub pull request"""
        async with httpx.AsyncClient() as client:
            data = {
                "title": title,
                "body": body,
                "head": head,
                "base": base
            }
            
            response = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=self.headers,
                json=data
            )
            
            if response.status_code not in [200, 201]:
                raise ValueError(f"Failed to create PR: {response.status_code}")
            
            return response.json()
    
    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        labels: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """List issues in a repository"""
        async with httpx.AsyncClient() as client:
            params = {"state": state}
            if labels:
                params["labels"] = ",".join(labels)
            
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                headers=self.headers,
                params=params
            )
            
            if response.status_code != 200:
                raise ValueError(f"Failed to list issues: {response.status_code}")
            
            return response.json()
    
    async def add_comment_to_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Add a comment to an issue"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=self.headers,
                json={"body": body}
            )
            
            if response.status_code != 201:
                raise ValueError(f"Failed to add comment: {response.status_code}")
            
            return response.json()
