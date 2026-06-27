"""Analytics API endpoints — Phase 15A-2 through 15A-6."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_current_admin_user, get_db
from app.services.analytics.execution_analytics_service import ExecutionAnalyticsService
from app.services.analytics.failure_analytics_service import FailureAnalyticsService
from app.services.analytics.knowledge_analytics_service import KnowledgeAnalyticsService
from app.services.analytics.operational_analytics_service import (
    OperationalAnalyticsService,
)
from app.services.analytics.operator_analytics_service import OperatorAnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/operational")
def get_operational_analytics(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Session success rate, task first-attempt rate, and failure category
    distribution across 7-day, 30-day, and all-time windows.

    Read-only. No writes. No caching. No background jobs.
    Sources: sessions, tasks, task_executions only.
    """
    return OperationalAnalyticsService(db).compute()


@router.get("/failures")
def get_failure_analytics(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Recovery attempts, success rates, budget exhaustion, churn guard
    activations, and failure category distribution across 7-day, 30-day,
    and all-time windows.

    Read-only. No writes. No caching. No background jobs.
    Sources: sessions, task_executions, orchestration event journal.
    """
    return FailureAnalyticsService(db).compute()


@router.get("/knowledge")
def get_knowledge_analytics(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Knowledge retrieval, prompt-injection, and effectiveness metrics across
    7-day, 30-day, and all-time windows.

    Read-only. No writes. No caching. No background jobs.
    Sources: knowledge_usage_logs, knowledge_items only.
    """
    return KnowledgeAnalyticsService(db).compute()


@router.get("/execution")
def get_execution_analytics(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Execution timing, queue latency, token usage, backend distribution, and
    phase-duration metrics across 7-day, 30-day, and all-time windows.

    Read-only. No writes. No caching. No background jobs.
    Sources: task_executions, orchestration event journal.
    """
    return ExecutionAnalyticsService(db).compute()


@router.get("/operators")
def get_operator_analytics(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Operator intervention rates, response latency, autonomy rate, pause/resume/stop
    counts, and intervention type distribution across 7-day, 30-day, and all-time windows.

    Read-only. No writes. No caching. No background jobs.
    Sources: intervention_requests, sessions only.
    """
    return OperatorAnalyticsService(db).compute()
