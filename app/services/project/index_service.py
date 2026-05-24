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
    ignored_dirs: list[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.monotonic)


PROJECT_STRUCTURE_CAPSULE_MAX_SOURCE_FILES = 80
PROJECT_STRUCTURE_CAPSULE_MAX_TEST_FILES = 40
PROJECT_STRUCTURE_CAPSULE_MAX_ENTRY_POINTS = 20
PROJECT_STRUCTURE_CAPSULE_MAX_PACKAGE_ROOTS = 20
PROJECT_STRUCTURE_CAPSULE_MAX_IGNORED_DIRS = 20
PROJECT_STRUCTURE_CAPSULE_MAX_CHARS = 2200


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
        ignored_dirs=sorted(_EXCLUDE_DIRS),
    )


def render_project_structure_capsule(
    project_index: ProjectIndex,
    *,
    max_source_files: int = PROJECT_STRUCTURE_CAPSULE_MAX_SOURCE_FILES,
    max_test_files: int = PROJECT_STRUCTURE_CAPSULE_MAX_TEST_FILES,
    max_entry_points: int = PROJECT_STRUCTURE_CAPSULE_MAX_ENTRY_POINTS,
    max_package_roots: int = PROJECT_STRUCTURE_CAPSULE_MAX_PACKAGE_ROOTS,
    max_ignored_dirs: int = PROJECT_STRUCTURE_CAPSULE_MAX_IGNORED_DIRS,
    max_chars: int = PROJECT_STRUCTURE_CAPSULE_MAX_CHARS,
) -> str:
    """Render a bounded read-only structural capsule for planning prompts."""

    source_cap = max(0, max_source_files)
    test_cap = max(0, max_test_files)
    entry_cap = max(0, max_entry_points)
    package_cap = max(0, max_package_roots)
    ignored_cap = max(0, max_ignored_dirs)

    while True:
        rendered = _render_capsule_with_caps(
            project_index,
            source_cap=source_cap,
            test_cap=test_cap,
            entry_cap=entry_cap,
            package_cap=package_cap,
            ignored_cap=ignored_cap,
        )
        if len(rendered) <= max_chars:
            return rendered

        reducible = {
            "source": source_cap,
            "test": test_cap,
            "entry": entry_cap,
            "package": package_cap,
            "ignored": ignored_cap,
        }
        target = max(reducible, key=reducible.get)
        if reducible[target] <= 5:
            return _truncate_capsule(rendered, max_chars)
        if target == "source":
            source_cap = max(5, source_cap - 5)
        elif target == "test":
            test_cap = max(5, test_cap - 5)
        elif target == "entry":
            entry_cap = max(5, entry_cap - 5)
        elif target == "package":
            package_cap = max(5, package_cap - 5)
        else:
            ignored_cap = max(5, ignored_cap - 5)


def _render_capsule_with_caps(
    project_index: ProjectIndex,
    *,
    source_cap: int,
    test_cap: int,
    entry_cap: int,
    package_cap: int,
    ignored_cap: int,
) -> str:
    lines = [
        "PROJECT STRUCTURE CAPSULE",
        "Use these paths as workspace facts. Do not create files outside this structure unless the task explicitly asks for new files.",
    ]
    lines.extend(_section_lines("Source files", project_index.source_files, source_cap))
    lines.extend(_section_lines("Tests", project_index.test_files, test_cap))
    lines.extend(_section_lines("Entry points", project_index.entry_points, entry_cap))
    lines.extend(
        _section_lines("Package roots", project_index.package_roots, package_cap)
    )
    lines.extend(_section_lines("Ignored", project_index.ignored_dirs, ignored_cap))
    return "\n".join(lines).strip()


def _section_lines(label: str, items: list[str], cap: int) -> list[str]:
    lines = [f"{label}:"]
    rendered_items = [str(item)[:180] for item in sorted(items)[:cap]]
    if not rendered_items:
        lines.append("- none")
        return lines
    lines.extend(f"- {item}" for item in rendered_items)
    omitted = max(0, len(items) - len(rendered_items))
    if omitted:
        lines.append(f"- ... {omitted} more {label.lower()} omitted")
    return lines


def _truncate_capsule(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n- ... capsule truncated to fit compact budget"
    if max_chars <= len(suffix):
        return suffix[-max_chars:]
    return text[: max_chars - len(suffix)].rstrip() + suffix
