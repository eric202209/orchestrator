"""Project-Type Authority (Phase 30C, Program 3).

A dedicated, metadata-only authority for a project's type classification.
It derives a type from existing observable repository signals, honors a
manual override when present, and exposes exactly one authoritative value.

This module never influences execution, planning, orchestration, or
provider selection — it is metadata only. Nothing here changes control
flow anywhere else in the system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

UNKNOWN_PROJECT_TYPE = "unknown"
KNOWN_PROJECT_TYPES: frozenset[str] = frozenset({"python", "node", "mixed"})

_PYTHON_MARKERS = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
_NODE_MARKER = "package.json"


def derive_project_type(project_dir: Optional[Path]) -> str:
    """Derive a project type from existing, observable repository markers.

    Returns a known type (``"python"``, ``"node"``, ``"mixed"``) or
    ``"unknown"``. Never returns a confidence score and never applies
    speculative/AI-based heuristics — presence of well-known manifest files
    only.
    """
    if project_dir is None:
        return UNKNOWN_PROJECT_TYPE
    try:
        directory = Path(project_dir)
        if not directory.is_dir():
            return UNKNOWN_PROJECT_TYPE
        is_python = any((directory / marker).exists() for marker in _PYTHON_MARKERS)
        is_node = (directory / _NODE_MARKER).exists()
    except OSError:
        return UNKNOWN_PROJECT_TYPE
    if is_python and is_node:
        return "mixed"
    if is_python:
        return "python"
    if is_node:
        return "node"
    return UNKNOWN_PROJECT_TYPE


def resolve_project_type(
    project_dir: Optional[Path], override: Optional[str] = None
) -> str:
    """Return the single authoritative project type.

    Manual override always wins when present and non-empty. Otherwise falls
    back to derivation; falls back to ``"unknown"`` when derivation cannot
    determine a known type.
    """
    normalized_override = str(override).strip() if override else ""
    if normalized_override:
        return normalized_override
    return derive_project_type(project_dir)
