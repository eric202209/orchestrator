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
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "openclaw-orchestrator",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"https://api.github.com{path}",
                headers=self.headers,
                params=params,
                json=json,
            )

        if response.status_code == 404:
            raise ValueError("GitHub resource not found")
        if response.status_code >= 400:
            raise ValueError(
                f"GitHub API error: {response.status_code} {response.text[:200]}"
            )

        return response.json()

    async def get_repository(self, owner: str, repo: str) -> Dict[str, Any]:
        """Get repository information"""
        try:
            return await self._request("GET", f"/repos/{owner}/{repo}")
        except ValueError as exc:
            if "not found" in str(exc).lower():
                raise ValueError(f"Repository {owner}/{repo} not found") from exc
            raise

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a GitHub issue"""
        data = {
            "title": title,
            "body": body,
            "labels": labels or [],
            "assignees": assignees or [],
        }
        return await self._request("POST", f"/repos/{owner}/{repo}/issues", json=data)

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> Dict[str, Any]:
        """Create a GitHub pull request"""
        data = {"title": title, "body": body, "head": head, "base": base}
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json=data)

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        labels: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """List issues in a repository"""
        params = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)

        return await self._request(
            "GET", f"/repos/{owner}/{repo}/issues", params=params
        )

    async def add_comment_to_issue(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> Dict[str, Any]:
        """Add a comment to an issue"""
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )

    async def get_pull_request(
        self, owner: str, repo: str, pr_number: int
    ) -> Dict[str, Any]:
        """Get a pull request by number."""
        try:
            return await self._request(
                "GET", f"/repos/{owner}/{repo}/pulls/{pr_number}"
            )
        except ValueError as exc:
            if "not found" in str(exc).lower():
                raise ValueError(f"Pull request #{pr_number} not found") from exc
            raise

    async def get_issue(
        self, owner: str, repo: str, issue_number: int
    ) -> Dict[str, Any]:
        """Get an issue by number."""
        try:
            return await self._request(
                "GET", f"/repos/{owner}/{repo}/issues/{issue_number}"
            )
        except ValueError as exc:
            if "not found" in str(exc).lower():
                raise ValueError(f"Issue #{issue_number} not found") from exc
            raise
