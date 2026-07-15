"""Run-start bootstrap preparation for orchestration task execution.

Captures build/runtime identity metadata and decides whether a task run
needs its own configured planning runtime, ahead of main phase dispatch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.config import settings
from app.services.agents.runtime_configuration import RoleRuntimeConfiguration


def env_value(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    text = value.strip()
    return text if text else None


def build_identity_snapshot() -> Dict[str, Optional[str]]:
    try:
        from app.services.observability.build_identity import _read_repo_git_sha

        repo_git_sha = _read_repo_git_sha() or "unknown"
    except Exception:
        repo_git_sha = "unknown"

    build_git_sha = (
        env_value("ORCHESTRATOR_GIT_SHA")
        or env_value("GIT_SHA")
        or env_value("COMMIT_SHA")
        or "unknown"
    )
    if build_git_sha != "unknown" and repo_git_sha != "unknown":
        stale_check = "ok" if build_git_sha == repo_git_sha else "stale"
    else:
        stale_check = "unknown"
    return {
        "version": str(settings.VERSION),
        "build_git_sha": build_git_sha,
        "repo_git_sha": repo_git_sha,
        "build_time": env_value("ORCHESTRATOR_BUILD_TIME") or env_value("BUILD_TIME"),
        "image_tag": env_value("ORCHESTRATOR_IMAGE_TAG") or env_value("IMAGE_TAG"),
        "image_id": env_value("ORCHESTRATOR_IMAGE_ID") or env_value("IMAGE_ID"),
        "stale_container_check": stale_check,
    }


def run_start_config_snapshot(
    db,
    runtime_selection: Dict[str, Any],
) -> Dict[str, Any]:
    """Capture non-secret run-start config provenance for replay bundles."""

    effective_agent_backend = runtime_selection.get("backend")
    effective_agent_model = runtime_selection.get("model_family")
    return {
        "source": "task_started_event",
        "values": {
            "AGENT_BACKEND": settings.AGENT_BACKEND,
            "PLANNING_BACKEND": settings.PLANNING_BACKEND or None,
            "EXECUTION_BACKEND": settings.EXECUTION_BACKEND or None,
            "REPAIR_BACKEND": settings.REPAIR_BACKEND or None,
            "DEBUG_REPAIR_BACKEND": settings.DEBUG_REPAIR_BACKEND or None,
            "AGENT_MODEL": settings.AGENT_MODEL,
            "PLANNER_MODEL": settings.PLANNER_MODEL or None,
            "EXECUTION_MODEL": settings.EXECUTION_MODEL or None,
            "DEBUG_REPAIR_MODEL": settings.DEBUG_REPAIR_MODEL or None,
            "PLANNING_REPAIR_MODEL": settings.PLANNING_REPAIR_MODEL,
            "PLANNING_REPAIR_ENABLED": settings.PLANNING_REPAIR_ENABLED,
            "PLANNING_REPAIR_DISABLE_THINKING": (
                settings.PLANNING_REPAIR_DISABLE_THINKING
            ),
            "DEBUG_REPAIR_DIRECT_ENABLED": settings.DEBUG_REPAIR_DIRECT_ENABLED,
            "DEBUG_REPAIR_DISABLE_THINKING": settings.DEBUG_REPAIR_DISABLE_THINKING,
            "WORKSPACE_REVIEW_POLICY": settings.WORKSPACE_REVIEW_POLICY,
            "INLINE_PLANNING": settings.INLINE_PLANNING,
        },
        "effective": {
            "agent_backend": effective_agent_backend,
            "agent_model": effective_agent_model,
            "planning_backend": runtime_selection.get("planner_backend"),
            "planning_model": runtime_selection.get("planner_model"),
            "execution_backend": runtime_selection.get("execution_backend"),
            "execution_model": runtime_selection.get("execution_model"),
            "repair_backend": settings.REPAIR_BACKEND or settings.AGENT_BACKEND,
            "debug_repair_backend": runtime_selection.get("debug_repair_backend"),
            "debug_repair_model": runtime_selection.get("debug_repair_model"),
        },
        "secret_fields_omitted": [
            "SECRET_KEY",
            "OPENAI_API_KEY",
            "OPENCLAW_API_KEY",
            "PLANNING_REPAIR_API_KEY",
            "DEBUG_REPAIR_API_KEY",
            "GITHUB_TOKEN",
            "MOBILE_GATEWAY_API_KEY",
        ],
    }


def run_start_runtime_identity(
    db,
    runtime_selection: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "source": "task_started_event",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "build": build_identity_snapshot(),
        "lanes": {
            "planning": runtime_selection.get("planner_backend"),
            "execution": runtime_selection.get("execution_backend"),
            "debug_repair": runtime_selection.get("debug_repair_backend"),
            "repair": settings.REPAIR_BACKEND or settings.AGENT_BACKEND,
        },
        "models": {
            "planner": runtime_selection.get("planner_model"),
            "execution": runtime_selection.get("execution_model"),
            "debug_repair": runtime_selection.get("debug_repair_model"),
            "planning_repair": settings.PLANNING_REPAIR_MODEL,
        },
        "config": run_start_config_snapshot(db, runtime_selection),
    }


def build_claimed_details(
    *,
    session_instance_id: Optional[str],
    expected_session_instance_id: Optional[str],
    celery_task_id: Optional[str],
    task_execution_id: Optional[int],
    dispatch_project_dir: Optional[Any],
    queue_latency_seconds: Optional[float],
    queued_event: Optional[Dict[str, Any]],
    runtime_selection: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the TASK_CLAIMED event/log details dict."""

    return {
        "session_instance_id": session_instance_id,
        "expected_session_instance_id": expected_session_instance_id,
        "celery_task_id": celery_task_id,
        "task_execution_id": task_execution_id,
        "project_dir": str(dispatch_project_dir) if dispatch_project_dir else None,
        "queue_latency_seconds": queue_latency_seconds,
        "queued_event_id": (queued_event or {}).get("event_id"),
        **runtime_selection,
    }


def should_use_configured_planning_runtime(
    *,
    planning_backend_override: Optional[str],
    planning_config: Optional[RoleRuntimeConfiguration] = None,
    execution_config: Optional[RoleRuntimeConfiguration] = None,
    resolved_planning_backend: Optional[str] = None,
    resolved_execution_backend: Optional[str] = None,
) -> bool:
    """Return whether planning needs its own configured runtime instance.

    Explicit role configurations are compared by all behavior-affecting
    fields, not by role or backend name alone. The string arguments remain a
    compatibility path for older diagnostics/tests; production worker calls
    pass the resolved configurations.
    """

    if planning_backend_override:
        return True
    if planning_config is not None and execution_config is not None:
        return not planning_config.is_behaviorally_equivalent(execution_config)
    planning_backend = str(resolved_planning_backend or "").strip()
    execution_backend = str(resolved_execution_backend or "").strip()
    return bool(planning_backend and planning_backend != execution_backend)
