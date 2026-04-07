"""GitHub-specific Celery tasks"""

import asyncio
import logging
from typing import Optional, Dict, Any
from app.celery_app import celery_app
from app.tasks.worker import get_db_session
from app.models import Session as SessionModel, Task, TaskStatus
from app.services.github_service import GitHubService

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Execute async GitHub client calls from Celery sync tasks."""
    return asyncio.run(coro)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_push_event(
    self, webhook_data: Dict[str, Any], repo_owner: str, repo_name: str, branch: str
):
    """
    Process GitHub push event

    Args:
        webhook_data: Webhook payload
        repo_owner: Repository owner
        repo_name: Repository name
        branch: Branch that was pushed to
    """
    db = get_db_session()

    try:
        # Get or create project
        from app.models import Project

        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )

        if not project:
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                branch=branch,
                description="Auto-created from GitHub webhook",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        # Create task for analysis
        from app.models import Task as TaskModel

        task = TaskModel(
            project_id=project.id,
            title=f"Analyze push to {branch}",
            description=f"Analyze changes in push event",
            status=TaskStatus.PENDING,
            priority=1,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # TODO: Analyze changes and create subtasks

        logger.info(f"Created task {task.id} for push event")

        return {
            "status": "processed",
            "project_id": project.id,
            "task_id": task.id,
            "branch": branch,
        }

    except Exception as exc:
        logger.error(f"Push event processing failed: {str(exc)}")
        raise self.retry(exc=exc)

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_pr_event(
    self, webhook_data: Dict[str, Any], repo_owner: str, repo_name: str, pr_number: int
):
    """
    Process GitHub PR event

    Args:
        webhook_data: Webhook payload
        repo_owner: Repository owner
        repo_name: Repository name
        pr_number: PR number
    """
    db = get_db_session()

    try:
        # Get or create project
        from app.models import Project

        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )

        if not project:
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        # Get PR details
        github_service = GitHubService()
        pr = _run_async(
            github_service.get_pull_request(repo_owner, repo_name, pr_number)
        )

        if not pr:
            raise ValueError(f"PR #{pr_number} not found")

        # Create task for PR review
        from app.models import Task as TaskModel

        task = TaskModel(
            project_id=project.id,
            title=f"Review PR #{pr_number}: {pr.get('title')}",
            description=f"Review pull request from {pr.get('user', {}).get('login')}",
            status=TaskStatus.PENDING,
            priority=2,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # TODO: Execute OpenClaw code review

        logger.info(f"Created task {task.id} for PR #{pr_number}")

        return {
            "status": "processed",
            "project_id": project.id,
            "task_id": task.id,
            "pr_number": pr_number,
        }

    except Exception as exc:
        logger.error(f"PR event processing failed: {str(exc)}")
        raise self.retry(exc=exc)

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_issue_event(
    self,
    webhook_data: Dict[str, Any],
    repo_owner: str,
    repo_name: str,
    issue_number: int,
):
    """
    Process GitHub issue event

    Args:
        webhook_data: Webhook payload
        repo_owner: Repository owner
        repo_name: Repository name
        issue_number: Issue number
    """
    db = get_db_session()

    try:
        # Get or create project
        from app.models import Project

        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )

        if not project:
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        # Get issue details
        github_service = GitHubService()
        issue = _run_async(
            github_service.get_issue(repo_owner, repo_name, issue_number)
        )

        if not issue:
            raise ValueError(f"Issue #{issue_number} not found")

        # Create task from issue
        from app.models import Task as TaskModel

        task = TaskModel(
            project_id=project.id,
            title=f"Handle issue #{issue_number}: {issue.get('title')}",
            description=issue.get("body", ""),
            status=TaskStatus.PENDING,
            priority=3,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # TODO: Execute OpenClaw to create fix

        logger.info(f"Created task {task.id} for issue #{issue_number}")

        return {
            "status": "processed",
            "project_id": project.id,
            "task_id": task.id,
            "issue_number": issue_number,
        }

    except Exception as exc:
        logger.error(f"Issue event processing failed: {str(exc)}")
        raise self.retry(exc=exc)

    finally:
        db.close()
