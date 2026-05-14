"""Workflow-phase profiles for orchestration planning and validation."""

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

WORKFLOW_PROFILE_MARKERS: Dict[str, Dict[str, List[str]]] = {
    "fullstack_scaffold": {
        "frontend": [
            "frontend",
            "front end",
            "client",
            "user interface",
            "browser",
            "web app",
        ],
        "backend": [
            "backend",
            "back end",
            "server",
            "api",
            "service",
            "requirements.txt",
            "python dependencies",
            "app/main.py",
        ],
        "scaffold": [
            "set up",
            "setup",
            "scaffold",
            "bootstrap",
            "create",
            "build",
            "initialize",
            "clean architecture",
        ],
        "wire_api_config": [
            "wire api config",
            "api config",
            "proxy",
            "cors",
            "client config",
            "environment config",
        ],
        "verify_dev_startup": [
            "dev-ready",
            "smoke check",
            "health",
            "routes",
            "lint",
            "type-check",
            "build",
            "verify_dev_startup",
        ],
        "frontend_skeleton_exclusions": [
            "lint",
            "smoke check",
            "dev-ready",
        ],
        "backend_skeleton_exclusions": [
            "smoke check",
            "dev-ready",
            "health",
            "routes",
        ],
    }
}

IMPLEMENTATION_INTENT_MARKERS = [
    "set up",
    "setup",
    "build",
    "create",
    "implement",
    "frontend",
    "backend",
    "client",
    "server",
    "api",
    "service",
    "web app",
]

MUTATION_BUILD_INTENT_MARKERS = [
    "add feature",
    "build",
    "create app",
    "create application",
    "frontend",
    "backend",
    "client",
    "server",
    "implement app",
    "scaffold",
    "source implementation",
    "update the api",
    "update the app",
]

MULTI_STACK_PAIR_MARKERS = [
    ("python", "javascript"),
    ("python", "node"),
    ("python", "typescript"),
    ("backend", "frontend"),
    ("server", "client"),
    ("api", "frontend"),
    ("api", "client"),
]


def get_workflow_phases(profile_name: str) -> List[str]:
    """Return configured workflow phases for a planning profile."""

    return list(WORKFLOW_PROFILES.get(profile_name, WORKFLOW_PROFILES["default"]))


def get_workflow_markers(profile_name: str) -> Dict[str, List[str]]:
    """Return marker groups for a workflow profile."""

    return {
        key: list(value)
        for key, value in WORKFLOW_PROFILE_MARKERS.get(profile_name, {}).items()
    }


def get_implementation_intent_markers() -> List[str]:
    return list(IMPLEMENTATION_INTENT_MARKERS)


def get_mutation_build_intent_markers() -> List[str]:
    return list(MUTATION_BUILD_INTENT_MARKERS)


def get_multi_stack_pair_markers() -> List[tuple[str, str]]:
    return list(MULTI_STACK_PAIR_MARKERS)
