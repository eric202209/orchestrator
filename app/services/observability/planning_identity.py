"""Creation-time identity snapshots for planning and execution records."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.services.observability.build_identity import build_identity_payload
from app.services.workspace.system_settings import get_effective_adaptation_profile


def _fingerprint(payload: dict[str, Any], reasoning_profile: str | None) -> str:
    """Hash stable, non-secret build/config identity fields only."""
    fingerprint_payload = {
        key: payload.get(key)
        for key in (
            "version",
            "git_sha",
            "build_git_sha",
            "repo_git_sha",
            "build_time",
            "image_tag",
            "image_id",
            "runtime_profile",
            "active_backend_lanes",
            "active_model_names",
            "config_source",
        )
    }
    fingerprint_payload["reasoning_profile"] = reasoning_profile
    encoded = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def active_planning_identity(db: Session) -> dict[str, str | None]:
    """Snapshot the active planning lane without retaining configuration secrets."""
    payload = build_identity_payload(db)
    reasoning_profile = get_effective_adaptation_profile(db=db) or None
    return {
        "planning_backend": payload["planning_backend"],
        "planner_model": payload["planner_model"],
        "reasoning_profile": reasoning_profile,
        "configuration_fingerprint": _fingerprint(payload, reasoning_profile),
    }


def active_execution_identity(db: Session) -> dict[str, str | None]:
    """Snapshot both role lanes at task-execution creation time."""
    payload = build_identity_payload(db)
    reasoning_profile = get_effective_adaptation_profile(db=db) or None
    return {
        "planning_backend": payload["planning_backend"],
        "execution_backend": payload["execution_backend"],
        "planner_model": payload["planner_model"],
        "executor_model": payload["execution_model"],
        "configuration_fingerprint": _fingerprint(payload, reasoning_profile),
    }
