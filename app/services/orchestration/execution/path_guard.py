"""Advisory telemetry for new top-level scaffold directories created by structured file ops.

Phase 1 — advisory only. No blocking, no task-outcome changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, NamedTuple

from app.services.orchestration.validation.workspace_checks import (
    NESTED_PROJECT_STRUCTURAL_DIRS,
)


class PathGuardAdvisory(NamedTuple):
    new_top_dir: str
    files_written: List[str]
    mode: str = "advisory"
    contract_violation_type: str = "nested_project_folder_created_advisory"


def _looks_like_nested_project_scaffold(root_name: str, paths: List[str]) -> bool:
    """Mirror of the inner heuristic in validator.py, applied to observed written paths.

    paths are relative to project_dir, e.g. ["mylib/__init__.py", "mylib/core.py"].
    root_name is the first path component, e.g. "mylib".
    """
    root_level_files = {Path(p).name for p in paths if len(Path(p).parts) == 2}
    second_level_dirs = {Path(p).parts[1] for p in paths if len(Path(p).parts) > 2}
    project_marker_files = {
        "package.json",
        "pyproject.toml",
        "setup.py",
        "vite.config.ts",
        "vite.config.js",
    }

    if root_level_files.intersection(project_marker_files):
        return True

    structural_dirs = second_level_dirs.intersection(NESTED_PROJECT_STRUCTURAL_DIRS)
    if len(structural_dirs) >= 2:
        return True

    return False


def detect_advisory_nested_scaffold(
    pre_step_checksum: Dict[str, str],
    files_changed: List[str],
) -> List[PathGuardAdvisory]:
    """Return advisory events for each new top-level directory written by structured ops
    that looks like a nested project scaffold.

    Does not block, raise, or alter execution. Returns an empty list when nothing is
    suspicious.

    Args:
        pre_step_checksum: output of compute_workspace_checksum() taken before execute_file_ops().
            Keys are relative paths that existed before the step ran.
        files_changed: list of relative paths returned by execute_file_ops() as "files_changed".
    """
    if not files_changed:
        return []

    # Top-level directory names present before the step
    pre_top_dirs = {
        Path(p).parts[0] for p in pre_step_checksum if len(Path(p).parts) > 1
    }

    # Group files_changed by new top-level dir
    new_top_dirs: Dict[str, List[str]] = {}
    for rel_path in files_changed:
        parts = Path(rel_path).parts
        if len(parts) < 2:
            continue  # top-level file, no directory created
        top = parts[0]
        if top.startswith("."):
            continue  # hidden dir (.github, .venv, etc.)
        if top in pre_top_dirs:
            continue  # pre-existing dir — in-place work, not a new scaffold root
        new_top_dirs.setdefault(top, []).append(rel_path)

    advisories: List[PathGuardAdvisory] = []
    for top_dir, written_paths in sorted(new_top_dirs.items()):
        # Mirror the plan-time validator's minimum-file guard: a single-file write
        # is not sufficient evidence of a full scaffold, regardless of depth.
        if len(written_paths) < 3:
            continue
        if _looks_like_nested_project_scaffold(top_dir, written_paths):
            advisories.append(
                PathGuardAdvisory(
                    new_top_dir=top_dir,
                    files_written=written_paths,
                )
            )

    return advisories
