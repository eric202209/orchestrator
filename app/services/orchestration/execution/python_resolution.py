from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PYTHON_SOURCE_PROJECT_DOT_VENV = "project_dot_venv"
PYTHON_SOURCE_PROJECT_VENV = "project_venv"
PYTHON_SOURCE_WORKER = "worker"


class ProjectPythonResolutionError(RuntimeError):
    """No valid interpreter could be selected for project verification."""


@dataclass(frozen=True)
class ResolvedPython:
    path: str
    source: str


def resolve_project_python_selection(project_dir: Path) -> ResolvedPython:
    """Resolve the interpreter verification should use for a project.

    Preference order:
    1. Project-local `.venv/bin/python`
    2. Project-local `venv/bin/python`
    3. The running Orchestrator Worker interpreter (`sys.executable`)
    4. ProjectPythonResolutionError

    A disposable Runtime Workspace normally has no local virtualenv. Falling
    back to PATH discovery there can select an unrelated system Python, while
    Candidate Validator runs under the worker interpreter. The worker is the
    shared fallback authority for both paths; PATH is never searched.

    A project interpreter path that exists but is a dangling symlink or not an
    executable file fails resolution instead of silently falling through to a
    lower-priority interpreter. Resolution only inspects the filesystem; it
    never executes an interpreter. Interpreter symlinks are not resolved so a
    venv keeps its own identity.
    """

    if not Path(project_dir).is_absolute():
        raise ProjectPythonResolutionError(
            f"project_dir must be absolute: {project_dir}"
        )
    root = Path(project_dir).resolve()

    for candidate, source in (
        (root / ".venv" / "bin" / "python", PYTHON_SOURCE_PROJECT_DOT_VENV),
        (root / "venv" / "bin" / "python", PYTHON_SOURCE_PROJECT_VENV),
    ):
        if not os.path.lexists(candidate):
            continue
        if not candidate.exists():
            raise ProjectPythonResolutionError(
                f"project interpreter is a dangling symlink: {candidate}"
            )
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ProjectPythonResolutionError(
                f"project interpreter is not an executable file: {candidate}"
            )
        return _record(ResolvedPython(path=str(candidate), source=source), root)

    worker = sys.executable
    if (
        not worker
        or not os.path.isabs(worker)
        or not os.path.isfile(worker)
        or not os.access(worker, os.X_OK)
    ):
        raise ProjectPythonResolutionError(
            f"no project virtualenv and Worker interpreter is invalid: {worker!r}"
        )
    return _record(ResolvedPython(path=worker, source=PYTHON_SOURCE_WORKER), root)


def _record(resolved: ResolvedPython, project_dir: Path) -> ResolvedPython:
    logger.info(
        "project_python_resolved",
        extra={
            "python_source": resolved.source,
            "python_path": resolved.path,
            "project_dir": str(project_dir),
        },
    )
    return resolved


def resolve_project_python(project_dir: Path) -> str:
    """Return the resolved interpreter path; see resolve_project_python_selection."""

    return resolve_project_python_selection(project_dir).path
