"""Read-only structural index of a project workspace."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

_EXCLUDE_DIRS = {
    ".git",
    ".mypy_cache",
    ".openclaw",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "node_modules",
    "venv",
}

_ENTRY_POINT_NAMES = {
    "main.py",
    "manage.py",
    "app.py",
    "setup.py",
    "pyproject.toml",
    "package.json",
    "index.js",
    "index.ts",
}

_TEST_PATTERNS = ("test_", "_test.", ".test.")


@dataclass
class ProjectIndex:
    project_dir: Path
    source_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    package_roots: list[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.monotonic)


def build_project_index(project_dir: Path) -> ProjectIndex:
    source_files: list[str] = []
    test_files: list[str] = []
    entry_points: list[str] = []
    package_roots: list[str] = []

    for path in sorted(project_dir.rglob("*")):
        if not path.exists():
            continue
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(part in _EXCLUDE_DIRS for part in relative.parts):
            continue

        if path.is_dir():
            if (path / "__init__.py").exists():
                package_roots.append(str(relative))
            continue

        rel_str = str(relative)
        name = path.name

        if name in _ENTRY_POINT_NAMES:
            entry_points.append(rel_str)

        if any(pat in name for pat in _TEST_PATTERNS):
            test_files.append(rel_str)
        else:
            source_files.append(rel_str)

    return ProjectIndex(
        project_dir=project_dir,
        source_files=sorted(source_files),
        test_files=sorted(test_files),
        entry_points=sorted(entry_points),
        package_roots=sorted(package_roots),
    )
