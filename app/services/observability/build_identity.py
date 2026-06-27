"""Read-only deployment/build identity diagnostics."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import BASE_DIR, LEGACY_ENV_ALIASES, settings
from app.db_migrations import MIGRATIONS
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)

UNKNOWN = "unknown"


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return UNKNOWN


def _repo_sha_from_env() -> Optional[str]:
    value = _env("ORCHESTRATOR_REPO_GIT_SHA", "REPO_GIT_SHA")
    return None if value == UNKNOWN else value


def _read_repo_git_sha(repo_root: Path = BASE_DIR) -> Optional[str]:
    env_sha = _repo_sha_from_env()
    if env_sha:
        return env_sha
    git_path = repo_root / ".git"
    try:
        if git_path.is_file():
            content = git_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                git_path = (repo_root / content.split(":", 1)[1].strip()).resolve()
            else:
                return None
        head = git_path / "HEAD"
        if not head.exists():
            return None
        head_content = head.read_text(encoding="utf-8").strip()
        if head_content.startswith("ref:"):
            ref_path = git_path / head_content.split(" ", 1)[1]
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()
            packed_refs = git_path / "packed-refs"
            if packed_refs.exists():
                ref_name = head_content.split(" ", 1)[1]
                for line in packed_refs.read_text(encoding="utf-8").splitlines():
                    if not line or line.startswith("#") or line.startswith("^"):
                        continue
                    sha, _, ref = line.partition(" ")
                    if ref == ref_name:
                        return sha.strip()
            return None
        return head_content or None
    except OSError:
        return None


def _stale_container_check(build_sha: str, repo_sha: Optional[str]) -> str:
    if build_sha == UNKNOWN or not repo_sha:
        return UNKNOWN
    return "ok" if build_sha == repo_sha else "stale"


def _stale_container_warning(
    stale_check: str, build_sha: str, repo_sha: Optional[str]
) -> Optional[str]:
    if stale_check != "stale":
        return None
    return (
        f"Container image SHA ({build_sha}) differs from repo HEAD "
        f"({repo_sha or 'unknown'}). "
        "Running code may not match the deployed image. "
        "Rebuild or redeploy to synchronize."
    )


_TIMEOUT_ENV_KEYS = (
    "PLANNING_REPAIR_TIMEOUT_SECONDS",
    "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
    "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
    "REPLAN_SYNTHESIS_TIMEOUT_SECONDS",
    "OLLAMA_PLANNING_TIMEOUT_SECONDS",
)


def _timeout_settings() -> Dict[str, Any]:
    sources = {
        k: ("env" if os.environ.get(k) else "default") for k in _TIMEOUT_ENV_KEYS
    }
    return {
        "planning_repair_timeout_seconds": settings.PLANNING_REPAIR_TIMEOUT_SECONDS,
        "planning_synthesis_timeout_seconds": settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS,
        "replan_synthesis_timeout_seconds": settings.REPLAN_SYNTHESIS_TIMEOUT_SECONDS,
        "ollama_planning_timeout_seconds": settings.OLLAMA_PLANNING_TIMEOUT_SECONDS,
        "planning_direct_local_openclaw_timeout_seconds": (
            settings.PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS
        ),
        "timeout_config_sources": sources,
    }


_PRIMARY_CONFIG_KEYS = (
    "AGENT_BACKEND",
    "AGENT_MODEL",
    "PLANNING_BACKEND",
    "EXECUTION_BACKEND",
    "DEBUG_REPAIR_BACKEND",
    "REPAIR_BACKEND",
    "PLANNING_REPAIR_MODEL",
    "DEBUG_REPAIR_MODEL",
    "RUNTIME_PROFILE",
    "OLLAMA_AGENT_MODEL",
)


def _config_source_summary() -> Dict[str, Any]:
    active_legacy: list[str] = [
        f"{legacy} → {current}"
        for legacy, current in LEGACY_ENV_ALIASES.items()
        if os.environ.get(legacy)
    ]
    explicitly_set = [k for k in _PRIMARY_CONFIG_KEYS if os.environ.get(k)]
    return {
        "sources": ["environment", ".env"],
        "explicitly_set_env_vars": explicitly_set,
        "active_legacy_aliases": active_legacy,
        "legacy_aliases_in_use": len(active_legacy) > 0,
    }


def _migration_identity(db: Session) -> Dict[str, Any]:
    expected = MIGRATIONS[-1].version if MIGRATIONS else UNKNOWN
    try:
        rows = db.execute(
            text("SELECT version FROM schema_migrations ORDER BY version")
        ).fetchall()
    except SQLAlchemyError as exc:
        return {
            "migration_version": UNKNOWN,
            "migration_count": 0,
            "expected_migration_version": expected,
            "migration_status": UNKNOWN,
            "migration_error": str(exc),
        }

    versions = [str(row[0]) for row in rows]
    latest = versions[-1] if versions else UNKNOWN
    return {
        "migration_version": latest,
        "migration_count": len(versions),
        "expected_migration_version": expected,
        "migration_status": "ok" if expected in versions else "pending",
    }


def _lane_identity(db: Session) -> Dict[str, Any]:
    effective_backend = get_effective_agent_backend(settings.AGENT_BACKEND, db=db)
    effective_model = get_effective_agent_model_family(settings.AGENT_MODEL, db=db)
    return {
        "active_backend_lanes": {
            "planning": settings.PLANNING_BACKEND or effective_backend,
            "execution": settings.EXECUTION_BACKEND or effective_backend,
            "debug_repair": (
                settings.DEBUG_REPAIR_BACKEND
                or settings.REPAIR_BACKEND
                or effective_backend
            ),
            "repair": settings.REPAIR_BACKEND or effective_backend,
        },
        "active_model_names": {
            "planner": settings.PLANNER_MODEL or effective_model,
            "execution": settings.EXECUTION_MODEL or effective_model,
            "debug_repair": (
                settings.DEBUG_REPAIR_MODEL
                or settings.PLANNING_REPAIR_MODEL
                or effective_model
            ),
            "planning_repair": settings.PLANNING_REPAIR_MODEL or effective_model,
        },
    }


def build_identity_payload(
    db: Session,
    *,
    repo_sha_provider: Callable[[], Optional[str]] = _read_repo_git_sha,
) -> Dict[str, Any]:
    """Return build/runtime identity without changing runtime behavior."""
    repo_sha = repo_sha_provider()
    build_sha = _env("ORCHESTRATOR_GIT_SHA", "GIT_SHA", "COMMIT_SHA")
    lanes = _lane_identity(db)
    migration = _migration_identity(db)
    active_backend_lanes = lanes["active_backend_lanes"]
    active_model_names = lanes["active_model_names"]
    stale_check = _stale_container_check(build_sha, repo_sha)
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "version": settings.VERSION,
        "git_sha": build_sha if build_sha != UNKNOWN else (repo_sha or UNKNOWN),
        "build_git_sha": build_sha,
        "repo_git_sha": repo_sha or UNKNOWN,
        "build_time": _env("ORCHESTRATOR_BUILD_TIME", "BUILD_TIME"),
        "image_tag": _env("ORCHESTRATOR_IMAGE_TAG", "IMAGE_TAG"),
        "image_id": _env("ORCHESTRATOR_IMAGE_ID", "IMAGE_ID"),
        **migration,
        "runtime_profile": settings.RUNTIME_PROFILE,
        "timeout_settings": _timeout_settings(),
        "planning_backend": active_backend_lanes["planning"],
        "execution_backend": active_backend_lanes["execution"],
        "debug_repair_backend": active_backend_lanes["debug_repair"],
        "repair_backend": active_backend_lanes["repair"],
        "planner_model": active_model_names["planner"],
        "execution_model": active_model_names["execution"],
        "debug_repair_model": active_model_names["debug_repair"],
        "planning_repair_model": active_model_names["planning_repair"],
        "active_backend_lanes": active_backend_lanes,
        "active_model_names": active_model_names,
        "config_source": _env("ORCHESTRATOR_CONFIG_SOURCE"),
        "config_sources": ["environment", ".env"],
        "config_source_summary": _config_source_summary(),
        "stale_container_check": stale_check,
        "stale_container_warning": _stale_container_warning(
            stale_check, build_sha, repo_sha
        ),
    }
