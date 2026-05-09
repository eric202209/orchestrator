"""Workflow-phase profiles for orchestration planning."""

from __future__ import annotations

from typing import Dict, List

WORKFLOW_PROFILES: Dict[str, List[str]] = {
    "fullstack_scaffold": [
        "create_frontend_skeleton",
        "create_backend_skeleton",
        "wire_api_config",
        "verify_dev_startup",
    ],
    "frontend_only": [
        "create_frontend_skeleton",
        "verify_dev_startup",
    ],
    "backend_only": [
        "create_backend_skeleton",
        "verify_dev_startup",
    ],
    "review_only": [
        "inspect_structure",
        "produce_report",
    ],
    "debug_only": [
        "reproduce_bug",
        "fix",
        "verify_fix",
    ],
    "default": [],
}


def get_workflow_phases(profile_name: str) -> List[str]:
    """Return configured workflow phases for a planning profile."""

    return list(WORKFLOW_PROFILES.get(profile_name, WORKFLOW_PROFILES["default"]))
