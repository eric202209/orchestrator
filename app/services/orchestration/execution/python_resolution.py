from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_project_python(project_dir: Path) -> str:
    """Resolve the interpreter verification should use for a project.

    Preference order:
    1. Project-local `.venv/bin/python`
    2. Project-local `venv/bin/python`
    3. The running Orchestrator interpreter

    A disposable Runtime Workspace normally has no local virtualenv. Falling
    back to PATH discovery there can select an unrelated system Python, while
    Candidate Validator runs under the worker interpreter. The worker is the
    shared fallback authority for both paths.
    """

    for candidate in (
        project_dir / ".venv" / "bin" / "python",
        project_dir / "venv" / "bin" / "python",
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    return sys.executable or "python3"
